"""Order planner: matched symbols -> order intents (place, or skip with a reason).

Pure logic: the same inputs always give the same plan, so a preview shows exactly what a run would do.
Guards are applied in rank order; the budget is spent top rank first.
"""

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from ..orders.service import PRICE_BUFFER, OrderRejected, check_request
from .models import OrderGuards, PlaceOrderAction, RankingKey, SelectionMode, SizingMode

D0 = Decimal(0)


@dataclass
class Holding:
    """The budget's shares of one symbol (bought by any of its automations): quantity and what they cost in total.

    Used by: PlanContext.holdings, _rank_value, plan; runner.Trading.context (built).
    """
    quantity: Decimal
    cost_basis: Decimal


@dataclass
class PlanContext:
    """Everything the planner needs about one budget, gathered by the runner once per run (shared by all order
    actions of the run).

    Attributes:
        snapshots: ticker -> market snapshot (latestPrice, dailyChangePercent, dailyVolume).
        holdings: ticker -> Holding (the budget's shares).
        open_buys: tickers with an open buy order paid from the budget (for skipBuyIfHeld).
        open_sells: ticker -> the budget's shares already being sold.
        budget_available: budget available when the run starts (base for PERCENT_OF_BUDGET).
        budget_equity: budget cash + invested (base for maxPositionPercent).
        session: market session at run time (PRE_MARKET, REGULAR, AFTER_HOURS, CLOSED).
        data_age_minutes: how old the market data is.
        orders_planned: PLACE intents for this budget so far in this run (for maxOrdersPerRun).
        budget_left: budget still unspent by planned buys (top rank first).

    Used by: plan; runner.Trading.context (built).
    """

    snapshots: dict[str, dict]  # ticker -> market snapshot (latestPrice, dailyChangePercent, dailyVolume)
    holdings: dict[str, Holding]  # the budget's shares
    open_buys: set[str]  # tickers with an open buy order paid from the budget
    open_sells: dict[str, Decimal]  # ticker -> the budget's shares already being sold
    budget_available: Decimal  # at the start of the run
    budget_equity: Decimal  # cash + invested (for maxPositionPercent)
    session: str | None
    data_age_minutes: int | None
    orders_planned: int = 0  # across all order actions of the run (for maxOrdersPerRun)
    budget_left: Decimal = field(init=False)

    def __post_init__(self):
        """Start the run with the whole available budget unspent."""
        self.budget_left = self.budget_available


def _price_tick(price: Decimal) -> Decimal:
    """Price precision: 0.0001 below $1, else 0.01.

    Args:
        price: a price.
    """
    return Decimal("0.0001") if price < 1 else Decimal("0.01")


def _round_price(price: Decimal) -> Decimal:
    """Round a computed limit/stop price to a valid tick.

    Args:
        price: the raw price.
    """
    return price.quantize(_price_tick(price))


def _rank_value(key: RankingKey, ticker: str, ctx: PlanContext) -> Decimal | None:
    """The value a symbol is ranked by (None = no data, ranked last).

    Args:
        key: the RankingKey.
        ticker: the symbol.
        ctx: snapshots and holdings.
    """
    snap = ctx.snapshots.get(ticker, {})
    price = snap.get("latestPrice")
    if key == RankingKey.LAST_PRICE:
        v = price
    elif key == RankingKey.DAILY_CHANGE_PERCENT:
        v = snap.get("dailyChangePercent")
    elif key == RankingKey.DAILY_VOLUME:
        v = snap.get("dailyVolume")
    else:
        h = ctx.holdings.get(ticker)
        if not h or not h.quantity or price is None:
            return None
        value = h.quantity * Decimal(str(price))
        if key == RankingKey.POSITION_VALUE:
            return value
        return (value - h.cost_basis) / h.cost_basis * 100 if h.cost_basis else None
    return Decimal(str(v)) if v is not None else None


def _intent(ticker, side, rank, sizing, **kw) -> dict:
    """A new intent dict (decision PLACE until a guard says otherwise).

    Args:
        ticker: the symbol.
        side: BUY or SELL.
        rank: 1-based rank in the selection.
        sizing: the OrderSizing used.
        kw: fields to override.
    """
    out = {"ticker": ticker, "side": side, "rank": rank, "sizing": sizing.model_dump(mode="json"), "quantity": 0,
           "estimatedPrice": None, "estimatedCost": None, "limitPrice": None, "stopPrice": None,
           "decision": "PLACE", "skipReason": None, "detail": None, "orderId": None}
    out.update(kw)
    return out


def _skip(intent: dict, reason: str, detail: str | None = None) -> dict:
    """Mark an intent SKIP with a reason.

    Args:
        intent: the intent.
        reason: a SkipReason code, e.g. ALREADY_HELD.
        detail: readable explanation.
    """
    intent.update(decision="SKIP", skipReason=reason, detail=detail)
    return intent


def plan(action: PlaceOrderAction, guards: OrderGuards, matched: list[str], ctx: PlanContext, broker) -> dict:
    """Turn matched symbols into order intents.

    Args:
        action: the PLACE_ORDER action.
        guards: the automation's OrderGuards.
        matched: matched tickers from the run's filters.
        ctx: PlanContext (mutated: orders_planned, budget_left).
        broker: the portfolio's Broker (capability checks only; nothing is sent).

    Returns:
        {"intents": [{ticker, side, rank, sizing, quantity, estimatedPrice, estimatedCost, limitPrice, stopPrice,
        decision (PLACE|SKIP), skipReason, detail, orderId}], "notHeld": matches a SELL can't use}.

    Used by: runner._place_orders.
    """
    # 1. Candidates: sells only from the budget's shares.
    if action.side == "SELL":
        candidates = [t for t in matched if ctx.holdings.get(t) and ctx.holdings[t].quantity > 0]
        not_held = len(matched) - len(candidates)
    else:
        candidates, not_held = list(matched), 0

    # 2. Rank and select.
    sel = action.selection
    if sel.mode == SelectionMode.TOP_N:
        valued = [(t, _rank_value(sel.rankBy, t, ctx)) for t in candidates]
        with_value = sorted([x for x in valued if x[1] is not None], key=lambda x: x[1], reverse=sel.descending)
        candidates = [t for t, _ in with_value + [x for x in valued if x[1] is None]][: sel.topN]
    else:
        candidates = sorted(candidates)

    intents = []
    for i, ticker in enumerate(candidates):
        rank = i + 1
        sizing = action.sizings[i] if len(action.sizings) > 1 else action.sizings[0]
        intent = _intent(ticker, action.side, rank, sizing)
        intents.append(intent)

        # 3. Whole-run guards.
        if ctx.data_age_minutes is None or ctx.data_age_minutes > guards.maxDataAgeMinutes:
            _skip(intent, "STALE_DATA", f"market data is {ctx.data_age_minutes} min old (max {guards.maxDataAgeMinutes})")
            continue
        if guards.regularHoursOnly and ctx.session != "REGULAR":
            _skip(intent, "OUTSIDE_REGULAR_HOURS", f"market session is {ctx.session}")
            continue

        snap_price = ctx.snapshots.get(ticker, {}).get("latestPrice")
        if snap_price is None:
            _skip(intent, "NO_PRICE")
            continue
        price = Decimal(str(snap_price))
        intent["estimatedPrice"] = float(price)
        held = ctx.holdings.get(ticker, Holding(D0, D0))

        # 4. Size.
        if action.side == "BUY":
            if sizing.mode == SizingMode.QUANTITY:
                qty = sizing.value
            elif sizing.mode == SizingMode.CASH_AMOUNT:
                qty = sizing.value / price
            else:
                qty = ctx.budget_available * sizing.value / 100 / price
        else:
            free = held.quantity - ctx.open_sells.get(ticker, D0)
            qty = min(sizing.value, free) if sizing.mode == SizingMode.QUANTITY else free * sizing.value / 100
        qty = qty.quantize(Decimal(1), rounding=ROUND_DOWN)

        # 5. Position guards (buys): never let one symbol grow past the caps; shrink the order to fit.
        if action.side == "BUY":
            if guards.skipBuyIfHeld and (held.quantity > 0 or ticker in ctx.open_buys):
                _skip(intent, "ALREADY_HELD", "the budget already holds it or has a buy order open")
                continue
            caps = []
            if guards.maxPositionValue is not None:
                caps.append(guards.maxPositionValue)
            if guards.maxPositionPercent is not None:
                caps.append(ctx.budget_equity * guards.maxPositionPercent / 100)
            if caps:
                room = min(caps) - held.quantity * price
                fit = (room / price).quantize(Decimal(1), rounding=ROUND_DOWN) if room > 0 else D0
                if fit < qty:
                    if fit <= 0:
                        _skip(intent, "MAX_POSITION", f"position limit {min(caps):.2f} reached")
                        continue
                    intent["detail"] = f"reduced from {qty} to {fit} shares by the position limit"
                    qty = fit

        if qty <= 0:
            _skip(intent, "ZERO_QUANTITY", "the sizing comes to less than one share")
            continue
        intent["quantity"] = float(qty)

        # 6. Prices for limit / stop orders.
        limit = stop = None
        if action.limitOffsetPercent is not None:
            limit = _round_price(price * (1 + action.limitOffsetPercent / 100))
        if action.stopOffsetPercent is not None:
            stop = _round_price(price * (1 + action.stopOffsetPercent / 100))
        intent["limitPrice"] = float(limit) if limit is not None else None
        intent["stopPrice"] = float(stop) if stop is not None else None

        try:
            check_request(broker, type=action.orderType, time_in_force=action.timeInForce, quantity=qty,
                          limit_price=limit, stop_price=stop, trail_percent=action.trailPercent,
                          extended_hours=action.extendedHours)
        except OrderRejected as e:
            _skip(intent, e.reason, str(e))
            continue

        # 7. Budget (buys, top rank first) and the per-run order cap.
        if action.side == "BUY":
            unit = limit if action.orderType in ("LIMIT", "STOP_LIMIT") else (stop or price) * (1 + PRICE_BUFFER)
            cost = (qty * unit).quantize(Decimal("0.01"))
            intent["estimatedCost"] = float(cost)
            if cost > ctx.budget_left:
                _skip(intent, "INSUFFICIENT_BUDGET", f"needs {cost}, {ctx.budget_left:.2f} left")
                continue
        else:
            intent["estimatedCost"] = float((qty * price).quantize(Decimal("0.01")))
        if guards.maxOrdersPerRun is not None and ctx.orders_planned >= guards.maxOrdersPerRun:
            _skip(intent, "MAX_ORDERS_REACHED", f"limit of {guards.maxOrdersPerRun} orders per run")
            continue

        ctx.orders_planned += 1
        if action.side == "BUY":
            ctx.budget_left -= cost
    return {"intents": intents, "notHeld": not_held}
