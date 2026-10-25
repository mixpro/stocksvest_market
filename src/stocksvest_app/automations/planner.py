"""Order planner: matched symbols -> order intents (place, or skip with a reason).

Pure logic: the same inputs always give the same plan, so a preview shows exactly what a run would do.
Guards are applied in rank order; the budget is spent top rank first.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from ..market.client import LIVE_SOURCES
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
        snapshots: ticker -> market snapshot (latestPrice, dailyChangePercent, dailyVolume), plus bid, ask,
            priceSource ("ALPACA" or "IBKR") and quoteAgeSeconds when the runner merged a live quote in.
        holdings: ticker -> Holding (the budget's shares).
        open_buys: tickers with an open buy order paid from the budget (for skipBuyIfHeld).
        open_sells: ticker -> the budget's shares already being sold.
        budget_available: budget available when the run starts (base for PERCENT_OF_BUDGET).
        budget_equity: budget cash + invested (base for maxPositionPercent).
        session: market session at run time (PRE_MARKET, REGULAR, AFTER_HOURS, CLOSED).
        data_age_minutes: how old the market data is.
        orders_planned: PLACE intents for this budget so far in this run (for maxOrdersPerRun).
        last_sells: ticker -> when the budget last sold it (for rebuyCooldownMinutes).
        now: the run time (for rebuyCooldownMinutes).
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
    last_sells: dict[str, datetime] = field(default_factory=dict)  # ticker -> the budget's last sell
    now: datetime | None = None
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


# Momentum ranking keys -> (months back, the snapshot field add_rank_data fills). Used by: add_rank_data, _rank_value
MOMENTUM = {RankingKey.MOMENTUM_12_1: (12, "momentum12_1"), RankingKey.MOMENTUM_6_1: (6, "momentum6_1")}


def add_rank_data(action: PlaceOrderAction, tickers: list[str], snapshots: dict[str, dict], market) -> None:
    """Put the ranking value market snapshots don't carry into the snapshots, in place: for a momentum TOP_N, each
    ticker's % change from N months ago to 1 month ago (None without both prices).

    Args:
        action: the order action (nothing to do unless it ranks by momentum).
        tickers: the candidates.
        snapshots: ticker -> snapshot (the PlanContext's).
        market: MarketData (price_changes).

    Used by: runner._place_orders, backtest.engine._run_automation (before select).
    """
    sel = action.selection
    if sel.mode != SelectionMode.TOP_N or sel.rankBy not in MOMENTUM or not tickers:
        return
    months, field_name = MOMENTUM[sel.rankBy]
    recent = market.price_changes(set(tickers), 1, "MONTHS")
    old = market.price_changes(set(tickers), months, "MONTHS")
    for t in tickers:
        a, b = recent.get(t, {}).get("fromPrice"), old.get(t, {}).get("fromPrice")
        snapshots[t] = {**snapshots.get(t, {}), field_name: (a / b - 1) * 100 if a and b else None}


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
    elif key in MOMENTUM:
        v = snap.get(MOMENTUM[key][1])
    else:
        h = ctx.holdings.get(ticker)
        if not h or not h.quantity or price is None:
            return None
        value = h.quantity * Decimal(str(price))
        if key == RankingKey.POSITION_VALUE:
            return value
        return (value - h.cost_basis) / h.cost_basis * 100 if h.cost_basis else None
    return Decimal(str(v)) if v is not None else None


def _sold_recently(ticker: str, guards: OrderGuards | None, ctx: PlanContext) -> datetime | None:
    """When the budget sold the ticker, if that is within the guards' rebuyCooldownMinutes (else None).

    Args:
        ticker: the symbol.
        guards: the automation's OrderGuards; None = no guards.
        ctx: PlanContext (last_sells, now).

    Used by: select, size.
    """
    if guards is None or guards.rebuyCooldownMinutes is None or ctx.now is None:
        return None
    sold = ctx.last_sells.get(ticker)
    return sold if sold and ctx.now - sold < timedelta(minutes=guards.rebuyCooldownMinutes) else None


def _spread_percent(snap: dict) -> Decimal | None:
    """A live quote's bid/ask spread as a % of its midpoint (None without a usable bid and ask).

    Args:
        snap: a snapshot with a live quote merged in (bid, ask).

    Used by: size (maxSpreadPercent).
    """
    bid, ask = snap.get("bid"), snap.get("ask")
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    bid, ask = Decimal(str(bid)), Decimal(str(ask))
    return (ask - bid) / ((ask + bid) / 2) * 100


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
           "estimatedPrice": None, "priceSource": None, "quoteAgeSeconds": None, "estimatedCost": None,
           "limitPrice": None, "stopPrice": None,
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


def select(action: PlaceOrderAction, matched: list[str], ctx: PlanContext, guards: OrderGuards | None = None) -> dict:
    """Which matched symbols the action acts on, in rank order. Needs no prices beyond the ranking key, so the
    runner can fetch live quotes for exactly these before size().

    Sells consider only symbols the budget holds. TOP_N ranks by action.selection.rankBy (on the snapshots the
    context has: the delayed market data) and keeps the best topN; ALL_MATCHES keeps every candidate, sorted.
    A TOP_N buy with skipBuyIfHeld leaves out what the budget already holds or has a buy open for *before* taking
    the top N, so "top 3" means the 3 best it doesn't own yet; likewise what it sold within rebuyCooldownMinutes.

    Args:
        action: the PLACE_ORDER action.
        matched: matched tickers from the run's filters.
        ctx: PlanContext (holdings, open buys and snapshots; not changed).
        guards: the automation's OrderGuards (skipBuyIfHeld); None = no guards.

    Returns:
        {"symbols": tickers in rank order, "notHeld": matches a SELL can't use,
        "alreadyHeld": matches a TOP_N buy left out because the budget holds them,
        "recentlySold": matches a TOP_N buy left out because the budget sold them within the cooldown}.

    Used by: plan, runner._place_orders.
    """
    # Candidates: sells only from the budget's shares.
    if action.side == "SELL":
        candidates = [t for t in matched if ctx.holdings.get(t) and ctx.holdings[t].quantity > 0]
        not_held = len(matched) - len(candidates)
    else:
        candidates, not_held = list(matched), 0
    already_held, recently_sold = [], []
    if action.side == "BUY" and action.selection.mode == SelectionMode.TOP_N and guards is not None:
        if guards.skipBuyIfHeld:
            held = {t for t, h in ctx.holdings.items() if h.quantity > 0} | ctx.open_buys
            already_held = sorted(t for t in candidates if t in held)
            candidates = [t for t in candidates if t not in held]
        recently_sold = sorted(t for t in candidates if _sold_recently(t, guards, ctx))
        candidates = [t for t in candidates if t not in recently_sold]

    # Rank and select.
    sel = action.selection
    if sel.mode == SelectionMode.TOP_N:
        valued = [(t, _rank_value(sel.rankBy, t, ctx)) for t in candidates]
        with_value = sorted([x for x in valued if x[1] is not None], key=lambda x: x[1], reverse=sel.descending)
        candidates = [t for t, _ in with_value + [x for x in valued if x[1] is None]][: sel.topN]
    else:
        candidates = sorted(candidates)
    return {"symbols": candidates, "notHeld": not_held, "alreadyHeld": already_held, "recentlySold": recently_sold}


def market_blocked(guards: OrderGuards, ctx: PlanContext) -> tuple[str, str] | None:
    """A whole-run reason to place nothing: market data too old, or outside the regular session when the guards
    ask for it.

    Args:
        guards: the automation's OrderGuards.
        ctx: PlanContext (data age and session).

    Returns:
        (skipReason, detail), or None when orders may go ahead.

    Used by: size, runner._place_orders (no live quotes are fetched when nothing can be placed anyway).
    """
    if ctx.data_age_minutes is None or ctx.data_age_minutes > guards.maxDataAgeMinutes:
        return "STALE_DATA", f"market data is {ctx.data_age_minutes} min old (max {guards.maxDataAgeMinutes})"
    if guards.regularHoursOnly and ctx.session != "REGULAR":
        return "OUTSIDE_REGULAR_HOURS", f"market session is {ctx.session}"
    return None


def size(action: PlaceOrderAction, guards: OrderGuards, symbols: list[str], ctx: PlanContext, broker) -> list[dict]:
    """Price, size and check the selected symbols in rank order: one intent each, PLACE or SKIP with a reason.

    Prices: a live quote merged into ctx.snapshots by the runner (buys at the ask, sells at the bid), else the
    delayed price. The budget is spent top rank first.

    Args:
        action: the PLACE_ORDER action.
        guards: the automation's OrderGuards.
        symbols: select()'s symbols, in rank order.
        ctx: PlanContext (mutated: orders_planned, budget_left).
        broker: the portfolio's Broker (capability checks only; nothing is sent).

    Returns:
        [{ticker, side, rank, sizing, quantity, estimatedPrice, priceSource, quoteAgeSeconds, estimatedCost,
        limitPrice, stopPrice, decision (PLACE|SKIP), skipReason, detail, orderId}].

    Used by: plan, runner._place_orders.
    """
    blocked = market_blocked(guards, ctx)
    candidates = symbols
    intents = []
    for i, ticker in enumerate(candidates):
        rank = i + 1
        sizing = action.sizings[i] if len(action.sizings) > 1 else action.sizings[0]
        intent = _intent(ticker, action.side, rank, sizing)
        intents.append(intent)

        # 1. Whole-run guards.
        if blocked:
            _skip(intent, *blocked)
            continue

        # Price: a live quote when the runner got one (buys at the ask, sells at the bid), else the delayed price.
        snap = ctx.snapshots.get(ticker, {})
        live = snap.get("priceSource") in LIVE_SOURCES
        intent["priceSource"] = snap["priceSource"] if live else "DELAYED"
        intent["quoteAgeSeconds"] = snap.get("quoteAgeSeconds")
        if guards.requireLivePrice and not live:
            _skip(intent, "NO_LIVE_PRICE", "no live quote right now (requireLivePrice)")
            continue
        spread = _spread_percent(snap) if live and action.side == "BUY" else None
        if guards.maxSpreadPercent is not None and spread is not None and spread > guards.maxSpreadPercent:
            _skip(intent, "WIDE_SPREAD", f"bid {snap['bid']} / ask {snap['ask']} is a {spread:.1f}% spread "
                                         f"(max {guards.maxSpreadPercent})")
            continue
        side_price = (snap.get("ask") if action.side == "BUY" else snap.get("bid")) if live else None
        snap_price = side_price if side_price is not None else snap.get("latestPrice")
        if snap_price is None:
            _skip(intent, "NO_PRICE")
            continue
        price = Decimal(str(snap_price))
        intent["estimatedPrice"] = float(price)
        held = ctx.holdings.get(ticker, Holding(D0, D0))

        # 2. Size.
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

        # 3. Position guards (buys): never let one symbol grow past the caps; shrink the order to fit.
        if action.side == "BUY":
            if guards.skipBuyIfHeld and (held.quantity > 0 or ticker in ctx.open_buys):
                _skip(intent, "ALREADY_HELD", "the budget already holds it or has a buy order open")
                continue
            sold = _sold_recently(ticker, guards, ctx)
            if sold:
                _skip(intent, "RECENTLY_SOLD", f"the budget sold it at {sold:%Y-%m-%d %H:%M} UTC "
                                               f"(cooldown {guards.rebuyCooldownMinutes} min)")
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

        # 4. Prices for limit / stop orders.
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

        # 5. Budget (buys, top rank first) and the per-run order cap.
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
    return intents


def plan(action: PlaceOrderAction, guards: OrderGuards, matched: list[str], ctx: PlanContext, broker) -> dict:
    """select() then size() on whatever prices the context holds (no live quotes are fetched here).

    Args:
        action: the PLACE_ORDER action.
        guards: the automation's OrderGuards.
        matched: matched tickers from the run's filters.
        ctx: PlanContext (mutated by size()).
        broker: the portfolio's Broker (capability checks only).

    Returns:
        {"intents": size()'s intents, "notHeld": select()'s notHeld}.

    Used by: tests and callers that plan without live prices (the runner calls select and size itself).
    """
    chosen = select(action, matched, ctx, guards)
    return {"intents": size(action, guards, chosen["symbols"], ctx, broker), "notHeld": chosen["notHeld"]}
