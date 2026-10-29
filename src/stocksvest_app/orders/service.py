"""Placing and cancelling orders, and applying what the broker reports (order updates and fills)
to orders, position lots, portfolio cash and budgets."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..audit import store as audit
from ..budgets import store as budget
from ..brokers.base import (
    FINAL_STATUSES,
    REJECTED,
    Broker,
    BrokerError,
    BrokerDividend,
    BrokerFee,
    BrokerFill,
    BrokerOrder,
    BrokerOrderRequest,
)
from ..db import immediate, iso, new_id, now_iso
from ..portfolios import store as pf
from ..portfolios.store import D, D0
from . import store

# Market and stop buys can fill above the last price; reserve a little extra and release the rest later.
# Used by: cost_basis_price, automations.planner.plan
PRICE_BUFFER = Decimal("0.01")
# A PENDING order the broker has never heard of after this long is treated as rejected. Used by: sync_account
PENDING_TIMEOUT = timedelta(minutes=5)
# Alpaca lists a fill a few seconds after the order status and balances show it. For this long after the broker's
# last change to an order, a fill it reports but hasn't listed yet defers the order update and the comparison.
FILL_LAG = timedelta(minutes=10)  # Used by: sync_account


class OrderRejected(ValueError):
    """The app refused the order before sending it.

    Attributes:
        reason: a SkipReason-style code (ZERO_QUANTITY, NOT_SUPPORTED_BY_BROKER, NOT_HELD, NO_PRICE,
            INSUFFICIENT_BUDGET, INVALID_ORDER).

    Used by: check_request, _check_funds, place_order, cancel_order (raised); automations.planner (-> SKIP),
    automations.runner, orders.routes.cancel and portfolios.routes.place_manual_order (-> 409/422).
    """

    def __init__(self, message: str, reason: str = "INVALID_ORDER"):
        """Args:
            message: readable explanation.
            reason: the code.
        """
        super().__init__(message)
        self.reason = reason


def check_request(broker: Broker, *, type: str, time_in_force: str, quantity: Decimal, limit_price, stop_price,
                  trail_percent, extended_hours: bool) -> None:
    """Check an order against the broker's capabilities and its own fields.

    Args:
        broker: the Broker (its capabilities).
        type: order type.
        time_in_force: time in force.
        quantity: shares above 0; fractions only for MARKET + DAY at a broker that takes them.
        limit_price: needed for LIMIT / STOP_LIMIT.
        stop_price: needed for STOP / STOP_LIMIT.
        trail_percent: needed for TRAILING_STOP.
        extended_hours: extended hours (LIMIT + DAY only).

    Raises:
        OrderRejected: the first problem found.

    Used by: place_order, automations.planner.plan (same checks in previews).
    """
    caps = broker.capabilities()
    if quantity <= 0:
        raise OrderRejected("quantity must be above 0", "ZERO_QUANTITY")
    if quantity != quantity.to_integral_value() and not (
            caps.fractional_shares and type == "MARKET" and time_in_force == "DAY"):
        raise OrderRejected("quantity must be a whole number of shares (fractions: MARKET + DAY only)", "ZERO_QUANTITY")
    if type not in caps.order_types:
        raise OrderRejected(f"{type} orders are not supported by this broker", "NOT_SUPPORTED_BY_BROKER")
    if time_in_force not in caps.time_in_force:
        raise OrderRejected(f"time in force {time_in_force} is not supported by this broker", "NOT_SUPPORTED_BY_BROKER")
    if extended_hours and not (caps.extended_hours and type == "LIMIT" and time_in_force == "DAY"):
        raise OrderRejected("extended-hours orders must be LIMIT + DAY", "NOT_SUPPORTED_BY_BROKER")
    if type in ("LIMIT", "STOP_LIMIT") and limit_price is None:
        raise OrderRejected(f"{type} needs a limit price")
    if type in ("STOP", "STOP_LIMIT") and stop_price is None:
        raise OrderRejected(f"{type} needs a stop price")
    if type == "TRAILING_STOP" and trail_percent is None:
        raise OrderRejected("TRAILING_STOP needs trailPercent")


def cost_basis_price(type: str, limit_price, stop_price, estimated_price) -> Decimal | None:
    """The price used to reserve money for a buy: the limit price, else the stop or estimated price + PRICE_BUFFER.

    Args:
        type: order type.
        limit_price: limit price.
        stop_price: stop price.
        estimated_price: last price when placed.

    Returns:
        Price per share, or None without any price.

    Used by: _check_funds, open_manual_buys.
    """
    if type in ("LIMIT", "STOP_LIMIT"):
        return D(limit_price)
    base = stop_price if type == "STOP" and stop_price is not None else estimated_price
    return D(base) * (1 + PRICE_BUFFER) if base is not None else None


def open_manual_buys(conn, portfolio_id: str) -> Decimal:
    """Money tied up by open manual buy orders (buys paid from a budget are covered by its reservation).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.

    Used by: budgets.store.unallocated_cash, performance.snapshots.portfolio_values, portfolios.routes.summary.
    """
    rows = conn.execute("SELECT * FROM orders WHERE portfolio_id = ? AND budget_id IS NULL AND side = 'BUY' "
                        "AND status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')", (portfolio_id,)).fetchall()
    total = D0
    for r in rows:
        price = cost_basis_price(r["type"], r["limit_price"], r["stop_price"], r["estimated_price"]) or D0
        total += (D(r["quantity"]) - D(r["filled_quantity"])) * price
    return total


def open_sell_quantity(conn, portfolio_id: str, symbol: str, budget_id: str | None) -> Decimal:
    """Shares already being sold by open sell orders of one owner.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        symbol: ticker.
        budget_id: the owning budget; None = the portfolio's manual shares.

    Used by: _check_funds.
    """
    rows = conn.execute("SELECT quantity, filled_quantity FROM orders WHERE portfolio_id = ? AND symbol = ? "
                        "AND budget_id IS ? AND side = 'SELL' AND status IN ('PENDING', 'SUBMITTED', "
                        "'PARTIALLY_FILLED')", (portfolio_id, symbol, budget_id)).fetchall()
    return sum((D(r["quantity"]) - D(r["filled_quantity"]) for r in rows), D0)


def _check_funds(conn, portfolio: dict, symbol: str, side: str, type: str, quantity: Decimal, limit_price, stop_price,
                 estimated_price, budget_id: str | None) -> Decimal:
    """Raise OrderRejected if the shares (sell) or money (buy) aren't there. Call inside db.immediate().

    Args:
        conn: app DB connection.
        portfolio: the portfolio dict.
        symbol: ticker.
        side: BUY or SELL.
        type: order type.
        quantity: shares.
        limit_price: limit price.
        stop_price: stop price.
        estimated_price: last price.
        budget_id: the budget paying / whose shares are sold, or None (unallocated cash, manual shares).

    Returns:
        The money to reserve (0 for sells).

    Raises:
        OrderRejected: NOT_HELD, NO_PRICE or INSUFFICIENT_BUDGET.

    Used by: place_order.
    """
    if side == "SELL":
        free = pf.held(conn, portfolio["id"], symbol, budget_id) - open_sell_quantity(conn, portfolio["id"], symbol,
                                                                                    budget_id)
        if quantity > free:
            raise OrderRejected(f"only {free} {symbol} shares are available to sell"
                                f"{' in this budget' if budget_id else ''}", "NOT_HELD")
        return D0
    price = cost_basis_price(type, limit_price, stop_price, estimated_price)
    if price is None:
        raise OrderRejected("no price to estimate the cost of this buy", "NO_PRICE")
    amount = (quantity * price).quantize(Decimal("0.01"))
    if budget_id:
        if amount > budget.available(conn, budget_id):
            raise OrderRejected(f"needs {amount}, budget has {budget.available(conn, budget_id):.2f}",
                                "INSUFFICIENT_BUDGET")
    elif amount > budget.unallocated_cash(conn, portfolio):
        raise OrderRejected(f"needs {amount}; not enough unallocated cash", "INSUFFICIENT_BUDGET")
    return amount


def place_order(conn: sqlite3.Connection, broker: Broker, portfolio: dict, *, symbol: str, side: str, type: str,
                quantity: Decimal, time_in_force: str, limit_price=None, stop_price=None, trail_percent=None,
                extended_hours: bool = False, automation_id: str | None = None, run_id: str | None = None,
                client_order_id: str | None = None, estimated_price=None, budget_id: str | None = None) -> dict:
    """Check, record, reserve budget, and send to the broker. Broker refusals are recorded as REJECTED
    orders; an unknown outcome leaves the order PENDING for the next sync. Every outcome goes to the activity log.

    Args:
        conn: app DB connection.
        broker: the portfolio's Broker.
        portfolio: the portfolio dict.
        symbol: ticker.
        side: BUY or SELL.
        type: order type.
        quantity: shares (fractions: MARKET + DAY only).
        time_in_force: time in force.
        limit_price: limit price.
        stop_price: stop price.
        trail_percent: trail percent.
        extended_hours: extended hours.
        automation_id: the automation placing it, or None for a manual order.
        run_id: the automation run that placed it.
        client_order_id: idempotency key; an existing order with it is returned unchanged. Default: a new id.
        estimated_price: last price, for the reservation and slippage.
        budget_id: the budget paying for a buy / whose shares a sell sells; None = unallocated cash and manual shares.

    Returns:
        The order.

    Raises:
        OrderRejected: refused by the app (nothing stored).

    Used by: automations.runner._place_orders, portfolios.routes.place_manual_order.
    """
    check_request(broker, type=type, time_in_force=time_in_force, quantity=quantity, limit_price=limit_price,
                  stop_price=stop_price, trail_percent=trail_percent, extended_hours=extended_hours)
    if client_order_id and store.get_order_row(conn, client_order_id=client_order_id):
        return store.get_order(conn, store.get_order_row(conn, client_order_id=client_order_id)["id"])  # idempotent

    client_order_id = client_order_id or new_id("sv")
    with immediate(conn):  # check what's available and reserve it under the write lock
        reserve_amount = _check_funds(conn, portfolio, symbol, side, type, quantity, limit_price, stop_price,
                                      estimated_price, budget_id)
        order_id = store.insert_order(
            conn, portfolio_id=portfolio["id"], broker_account_id=portfolio["brokerAccountId"], symbol=symbol,
            automation_id=automation_id, run_id=run_id, client_order_id=client_order_id, side=side, type=type,
            time_in_force=time_in_force, quantity=quantity, limit_price=limit_price, stop_price=stop_price,
            trail_percent=trail_percent, extended_hours=extended_hours, estimated_price=estimated_price,
            reserved=reserve_amount if budget_id else D0, budget_id=budget_id)
        if budget_id and reserve_amount:
            budget.reserve(conn, budget_id, reserve_amount, order_id, run_id, automation_id)

    request = BrokerOrderRequest(client_order_id=client_order_id, symbol=symbol, side=side, type=type,
                                 quantity=quantity, time_in_force=time_in_force,
                                 limit_price=D(limit_price) if limit_price is not None else None,
                                 stop_price=D(stop_price) if stop_price is not None else None,
                                 trail_percent=D(trail_percent) if trail_percent is not None else None,
                                 extended_hours=extended_hours)
    related = {"portfolioId": portfolio["id"], "budgetId": budget_id, "automationId": automation_id, "runId": run_id}
    details = {"symbol": symbol, "side": side, "type": type, "quantity": str(quantity), "timeInForce": time_in_force,
               "limitPrice": limit_price, "stopPrice": stop_price, "estimatedPrice": estimated_price,
               "reserved": str(reserve_amount), "clientOrderId": client_order_id}
    try:
        bo = broker.submit_order(request)
    except BrokerError as e:
        if not e.retryable:
            with conn:
                store.update_order(conn, order_id, status=REJECTED, reject_reason=str(e))
                finalize(conn, store.get_order_row(conn, id=order_id))
        audit.record(conn, "ORDER_REJECTED" if not e.retryable else "ORDER_SUBMIT_UNCERTAIN", "ORDER", order_id,
                     related=related, details=details, outcome="FAILED", error=str(e))
        # retryable: stays PENDING; the next broker sync finds it by client order id or rejects it after a timeout
        return store.get_order(conn, order_id)
    with conn:
        apply_order_update(conn, store.get_order_row(conn, id=order_id), bo, log=False)
    order = store.get_order(conn, order_id)
    audit.record(conn, "ORDER_PLACED" if order["status"] != REJECTED else "ORDER_REJECTED", "ORDER", order_id,
                 related=related, details={**details, "status": order["status"], "brokerOrderId": bo.broker_order_id},
                 outcome="OK" if order["status"] != REJECTED else "FAILED", error=order["rejectReason"])
    return order


def apply_order_update(conn, row: sqlite3.Row, bo: BrokerOrder, log: bool = True) -> bool:
    """Copy the broker's view of an order; finalize it when it reaches a final status.

    Args:
        conn: app DB connection.
        row: the order row.
        bo: the broker's BrokerOrder.
        log: record status changes in the activity log.

    Returns:
        True if anything changed.

    Used by: place_order, cancel_order, sync_account.
    """
    changed = (bo.status != row["status"] or str(bo.filled_quantity) != row["filled_quantity"]
               or bo.broker_order_id != row["broker_order_id"])
    if log and bo.status != row["status"]:
        audit.record(conn, f"ORDER_{bo.status}", "ORDER", row["id"], source="SYNC",
                     related={"portfolioId": row["portfolio_id"], "budgetId": row["budget_id"],
                              "automationId": row["automation_id"],
                              "runId": row["automation_run_id"]},
                     details={"from": row["status"], "to": bo.status, "rawStatus": bo.raw_status,
                              "filledQuantity": str(bo.filled_quantity), "averageFillPrice": bo.average_fill_price,
                              "symbol": row["symbol"], "side": row["side"]},
                     outcome="FAILED" if bo.status == REJECTED else "OK", error=bo.reject_reason)
    fields = dict(broker_order_id=bo.broker_order_id, status=bo.status, raw_status=bo.raw_status,
                  filled_quantity=bo.filled_quantity, reject_reason=bo.reject_reason,
                  average_fill_price=bo.average_fill_price)
    if bo.submitted_at and not row["submitted_at"]:
        fields["submitted_at"] = iso(bo.submitted_at)
    store.update_order(conn, row["id"], **fields)
    if bo.status in FINAL_STATUSES and row["status"] not in FINAL_STATUSES:
        finalize(conn, store.get_order_row(conn, id=row["id"]))
    return changed


def finalize(conn, row: sqlite3.Row) -> None:
    """An order reached a final status: stamp the time and give unused reserved budget back.

    Args:
        conn: app DB connection.
        row: the order row (already updated to its final status).

    Used by: place_order, apply_order_update, sync_account.
    """
    ts = now_iso()
    if row["status"] == "FILLED":
        last = conn.execute("SELECT MAX(executed_at) FROM fills WHERE order_id = ?", (row["id"],)).fetchone()[0]
        store.update_order(conn, row["id"], filled_at=last or ts)  # the broker's fill time, once imported
    else:
        store.update_order(conn, row["id"], cancelled_at=ts)
    left = D(row["reserved"])
    if row["budget_id"] and left > 0:
        budget.release(conn, row["budget_id"], left, row["id"], f"order {row['status'].lower()}", row["automation_id"])
        store.update_order(conn, row["id"], reserved=D0)


def apply_fill(conn, row: sqlite3.Row, fill: BrokerFill) -> list[dict]:
    """Record a fill of an app order and update lots, the portfolio's cash and the budget that paid / owns the shares.

    Args:
        conn: app DB connection.
        row: the order row.
        fill: the broker's BrokerFill.

    Returns:
        Mismatches (a sell of more shares than held), if any.

    Used by: sync_account.
    """
    portfolio_id, order_id = row["portfolio_id"], row["id"]
    owner, buyer = row["budget_id"], row["automation_id"]  # the budget owns shares and money; buyer is shown only
    fill_id = store.insert_fill(conn, order_id, portfolio_id, fill)
    audit.record(conn, "FILL_IMPORTED", "ORDER", order_id, source="SYNC",
                 related={"portfolioId": portfolio_id, "budgetId": owner, "automationId": buyer, "fillId": fill_id},
                 details={"symbol": fill.symbol, "side": fill.side, "quantity": str(fill.quantity),
                          "price": str(fill.price), "fee": str(fill.fee), "brokerFillId": fill.broker_fill_id})
    cost = fill.quantity * fill.price
    mismatches = []
    tags = {"automation_id": buyer, "budget_id": owner, "order_id": order_id, "fill_id": fill_id}
    if fill.side == "BUY":
        pf.change_cash(conn, portfolio_id, "BUY_FILL", -cost, **tags)
    else:
        pf.change_cash(conn, portfolio_id, "SELL_FILL", cost, **tags)
    if fill.fee:
        pf.change_cash(conn, portfolio_id, "FEE", -fill.fee, **tags)
    if fill.side == "BUY":
        pf.open_lot(conn, portfolio_id, fill.symbol, buyer, fill.quantity, fill.price, buy_fill_id=fill_id,
                    opened_at=iso(fill.executed_at), budget_id=owner)
        if owner:
            used = min(D(row["reserved"]), cost)
            budget.buy_fill(conn, owner, cost, used, row["id"], fill_id, buyer)
            store.update_order(conn, row["id"], reserved=D(row["reserved"]) - used)
    else:
        sold = {"fill_id": fill_id, "sold_at": iso(fill.executed_at)}
        taken, basis, pnl = pf.close_lots_fifo(conn, portfolio_id, fill.symbol, owner, fill.quantity, fill.price,
                                               **sold)
        if owner:
            budget.sell_fill(conn, owner, taken * fill.price, basis, pnl, row["id"], fill_id, buyer)
        if taken < fill.quantity:
            # Sold more than the budget held: take it from the portfolio's manual shares.
            extra = D0
            if owner:
                extra, _, extra_pnl = pf.close_lots_fifo(conn, portfolio_id, fill.symbol, None, fill.quantity - taken,
                                                         fill.price, **sold)
                pnl += extra_pnl
            if taken + extra < fill.quantity:
                mismatches.append({"kind": "POSITION_QUANTITY", "symbol": fill.symbol,
                                   "detail": f"sell fill of {fill.quantity} but only {taken + extra} shares were held"})
        store.set_fill_pnl(conn, fill_id, pnl)
    if owner and fill.fee:
        budget.fee(conn, owner, fill.fee, row["id"], fill_id, buyer)
    return mismatches


def split_cents(amount: Decimal, weights: dict) -> dict:
    """Split an amount by weights into cents that add up exactly (largest remainders get the leftover cents).

    Args:
        amount: what to split (may be negative).
        weights: key -> weight (> 0).

    Returns:
        key -> share, in cents.

    Used by: apply_account_fee, apply_dividend.
    """
    sign = -1 if amount < 0 else 1
    total = sum(weights.values())
    cents = abs(int((amount * 100).to_integral_value()))
    raw = {k: Decimal(cents) * Decimal(w) / Decimal(total) for k, w in weights.items()}
    out = {k: int(v.to_integral_value(rounding="ROUND_FLOOR")) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: cents - sum(out.values())]:
        out[k] += 1
    return {k: sign * Decimal(v) / 100 for k, v in out.items()}


def apply_account_fee(conn, account: dict, portfolios: list[dict], fee: BrokerFee) -> list[dict]:
    """Charge an account fee (e.g. the daily CAT fee for the day's trades) to whoever traded that day: split by
    number of fills per budget (or the portfolio's manual trades), in exact cents. The share of trades made outside
    the app stays with the account's unassigned cash. A fee for a day without app trades is split between the
    portfolios by their cash. Call inside db.immediate().

    Args:
        conn: app DB connection.
        account: the broker account.
        portfolios: the account's portfolios.
        fee: the fee.

    Returns:
        [{portfolioId, budgetId, amount}] that were charged.

    Used by: sync_account.
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    ids = {p["id"] for p in portfolios}
    weights: dict = {}
    rows = conn.execute("SELECT f.portfolio_id, o.budget_id, f.executed_at FROM fills f "
                        "LEFT JOIN orders o ON o.id = f.order_id").fetchall()
    for r in rows:
        if r["portfolio_id"] in ids and datetime.fromisoformat(r["executed_at"]).astimezone(et).date().isoformat() == fee.date:
            key = (r["portfolio_id"], r["budget_id"])
            weights[key] = weights.get(key, 0) + 1
    external = sum(1 for r in conn.execute("SELECT executed_at FROM external_fills WHERE broker_account_id = ?",
                                           (account["id"],))
                   if datetime.fromisoformat(r["executed_at"]).astimezone(et).date().isoformat() == fee.date)
    if external and weights:
        weights[None] = external  # outside trades: their share stays unassigned
    if not weights:  # no app trades that day: the portfolios pay by their cash
        weights = {(p["id"], None): D(p["cash"]) for p in portfolios if D(p["cash"]) > 0}
    if not weights:
        weights = {None: 1}
    charged = []
    for key, share in split_cents(fee.amount, weights).items():
        if key is None or not share:
            continue
        portfolio_id, budget_id = key
        note = f"{fee.description} ({fee.date})"
        pf.change_cash(conn, portfolio_id, "FEE", -share, budget_id=budget_id, note=note)
        if budget_id and budget.get_budget(conn, budget_id):
            budget.fee(conn, budget_id, share, None, None, None, note=note)
        charged.append({"portfolioId": portfolio_id, "budgetId": budget_id, "amount": float(share)})
    store.insert_fee(conn, account["id"], fee, charged)
    return charged


def apply_dividend(conn, account: dict, portfolios: list[dict], d: BrokerDividend) -> list[dict]:
    """Credit a dividend (or debit the tax withheld from it) to whoever held the shares: split by shares per budget
    (or the portfolio's manual shares), in exact cents. The share of shares held outside the app stays with the
    account's unassigned cash. Call inside db.immediate().

    Args:
        conn: app DB connection.
        account: the broker account.
        portfolios: the account's portfolios.
        d: the dividend.

    Returns:
        [{portfolioId, budgetId, amount}] that were credited.

    Used by: sync_account.
    """
    # ponytail: the broker gives the pay date, not the ex-date: holders = lots open then plus lots sold in the 30
    # days before (the usual ex-to-pay gap). Exact would need the ex-date from the corporate actions API.
    ids = {p["id"] for p in portfolios}
    sold_from = (datetime.fromisoformat(d.date) - timedelta(days=30)).date().isoformat()
    weights: dict = {}
    for r in conn.execute("SELECT portfolio_id, budget_id, quantity, remaining_quantity, closed_at FROM position_lots "
                          "WHERE symbol = ? AND opened_at < ? AND (closed_at IS NULL OR closed_at >= ?)",
                          (d.symbol, d.date, sold_from)):
        if r["portfolio_id"] in ids:
            q = D(r["remaining_quantity"]) if r["closed_at"] is None else D(r["quantity"])
            if q > 0:
                weights[(r["portfolio_id"], r["budget_id"])] = weights.get((r["portfolio_id"], r["budget_id"]), D0) + q
    held = sum(weights.values(), D0)
    if d.quantity and d.quantity > held:
        weights[None] = d.quantity - held  # shares held outside the app
    credited = []
    for key, share in split_cents(d.amount, weights or {None: 1}).items():
        if key is None or not share:
            continue
        portfolio_id, budget_id = key
        note = f"{d.symbol} {d.kind}: {d.description} ({d.date})"
        pf.change_cash(conn, portfolio_id, "DIVIDEND", share, budget_id=budget_id, note=note)
        if budget_id and budget.get_budget(conn, budget_id):
            budget.dividend(conn, budget_id, share, note)
        credited.append({"portfolioId": portfolio_id, "budgetId": budget_id, "amount": float(share)})
    store.insert_dividend(conn, account["id"], d, credited)
    return credited


def cancel_order(conn, broker: Broker, order_id: str) -> dict:
    """Ask the broker to cancel an open order and record its new state.

    Args:
        conn: app DB connection.
        broker: the portfolio's Broker.
        order_id: the order's id.

    Returns:
        The order.

    Raises:
        OrderRejected: already final, or not at the broker yet.
        BrokerError: the broker failed.

    Used by: orders.routes.cancel.
    """
    row = store.get_order_row(conn, id=order_id)
    if row["status"] in FINAL_STATUSES:
        raise OrderRejected(f"order is already {row['status'].lower()}")
    if not row["broker_order_id"]:
        raise OrderRejected("order has not reached the broker yet; try again after the next sync")
    broker.cancel_order(row["broker_order_id"])
    with conn:
        apply_order_update(conn, row, broker.get_order(row["broker_order_id"]))
    return store.get_order(conn, order_id)


def sync_account(conn, broker: Broker, account: dict) -> dict:
    """Sync one broker account and all its portfolios: balances, fills (assigned to portfolios through their
    orders; fills of orders placed outside the app are recorded as external), account fees (apply_account_fee),
    dividends (apply_dividend), order updates, then compare the app's positions and cash with the broker. Shares and cash not in any portfolio
    are "unassigned".

    Args:
        conn: app DB connection.
        broker: the portfolio's Broker.
        account: the broker account dict.

    Returns:
        The stored sync record (store.last_sync).

    Raises:
        BrokerError: the broker failed.

    Used by: run_account_sync.
    """
    from ..brokers import store as brokers_store

    snap = broker.account_snapshot()
    # Cash in no portfolio before this sync (may be negative), to find the broker's cent rounding below.
    loose_before = (D(str(account["cash"])) - pf.assigned_cash(conn, account["id"])
                    if account.get("cash") is not None else None)
    brokers_store.set_balances(conn, account["id"], snap.cash, snap.buying_power, snap.equity)
    portfolios = pf.list_portfolios(conn, account["id"])
    previous = store.last_sync(conn, account["id"])

    # Fills first, so final orders release only what their fills did not use.
    since_str = store.last_fill_time(conn, account["id"]) or account["createdAt"]
    since = datetime.fromisoformat(since_str) - timedelta(days=1)
    fills_imported = external = other_cash = 0
    traded: list[str] = []  # portfolio of each fill imported now
    mismatches = []
    for f in broker.list_fills(since):
        with immediate(conn):  # two syncs at once can't both apply the same fill
            if store.fill_exists(conn, f.broker_fill_id):
                continue
            row = store.get_order_row(conn, broker_order_id=f.broker_order_id)
            if row is None:
                store.insert_external_fill(conn, account["id"], f)
                audit.record(conn, "EXTERNAL_FILL", "BROKER_ACCOUNT", account["id"], source="SYNC",
                             details={"symbol": f.symbol, "side": f.side, "quantity": str(f.quantity),
                                      "price": str(f.price), "brokerFillId": f.broker_fill_id})
                external += 1
                continue
            mismatches += apply_fill(conn, row, f)
            traded.append(row["portfolio_id"])
        fills_imported += 1

    # Account fees booked apart from the fills (after the fills, so they can be split by who traded).
    # Only fees from since the account was added: older ones were before the app tracked its portfolios.
    tracked_from = account["createdAt"][:10]
    last_fee = store.last_fee_date(conn, account["id"]) or tracked_from
    fee_since = datetime.fromisoformat(last_fee).replace(tzinfo=timezone.utc) - timedelta(days=3)
    for fee in broker.list_fees(fee_since):
        if fee.date < tracked_from:
            continue
        with immediate(conn):
            if store.fee_exists(conn, fee.broker_fee_id):
                continue
            charged = apply_account_fee(conn, account, pf.list_portfolios(conn, account["id"]), fee)
            other_cash += 1
        audit.record(conn, "ACCOUNT_FEE", "BROKER_ACCOUNT", account["id"], source="SYNC",
                     details={"brokerFeeId": fee.broker_fee_id, "date": fee.date, "amount": str(fee.amount),
                              "description": fee.description, "chargedTo": charged})

    # Dividends and the tax withheld from them, from since the account was added (like the fees).
    last_div = store.last_dividend_date(conn, account["id"]) or tracked_from
    div_since = datetime.fromisoformat(last_div).replace(tzinfo=timezone.utc) - timedelta(days=3)
    for d in broker.list_dividends(div_since):
        if d.date < tracked_from:
            continue
        with immediate(conn):
            if store.dividend_exists(conn, d.broker_dividend_id):
                continue
            credited = apply_dividend(conn, account, pf.list_portfolios(conn, account["id"]), d)
            other_cash += 1
        audit.record(conn, "DIVIDEND", "BROKER_ACCOUNT", account["id"], source="SYNC",
                     details={"brokerDividendId": d.broker_dividend_id, "symbol": d.symbol, "kind": d.kind,
                              "date": d.date, "amount": str(d.amount), "creditedTo": credited})

    orders_updated = 0
    awaiting_fills = []  # symbols whose fills the broker reported but hasn't listed yet
    now = datetime.now(timezone.utc)
    for p in portfolios:
        for o in store.list_orders(conn, portfolio_id=p["id"], open_only=True, limit=1000):
            row = store.get_order_row(conn, id=o["id"])
            bo = (broker.get_order(row["broker_order_id"]) if row["broker_order_id"]
                  else broker.get_order_by_client_id(row["client_order_id"]))
            with conn:
                if bo is None:
                    if now - datetime.fromisoformat(row["created_at"]) > PENDING_TIMEOUT:
                        store.update_order(conn, row["id"], status=REJECTED, reject_reason="never reached the broker")
                        finalize(conn, store.get_order_row(conn, id=row["id"]))
                        orders_updated += 1
                    continue
                if (bo.filled_quantity > store.imported_quantity(conn, row["id"])
                        and bo.updated_at and now - bo.updated_at < FILL_LAG):
                    awaiting_fills.append(row["symbol"])  # the next sync imports the fill, then updates the order
                    continue
                orders_updated += apply_order_update(conn, row, bo)

    # Compare: the broker holds the app's shares plus unassigned ones; it can never hold fewer. Skipped while fills
    # are still to come: the broker's cash and positions already include them, the app's not yet.
    app_positions: dict[str, Decimal] = {}
    for p in portfolios:
        for x in pf.positions(conn, p["id"]):
            app_positions[x["symbol"]] = app_positions.get(x["symbol"], D0) + x["quantity"]
    broker_positions = {x.symbol: x.quantity for x in broker.positions()}
    unassigned_shares = {}
    for symbol in sorted(set(app_positions) | set(broker_positions)):
        a, b = app_positions.get(symbol, D0), broker_positions.get(symbol, D0)
        if b > a:
            unassigned_shares[symbol] = float(b - a)
        elif a > b and not awaiting_fills:
            mismatches.append({"kind": "POSITION_QUANTITY", "symbol": symbol, "appValue": float(a),
                               "brokerValue": float(b), "detail": "portfolios hold more shares than the broker"})
    assigned = pf.assigned_cash(conn, account["id"])
    if traded and loose_before is not None and not (external or other_cash or awaiting_fills):
        # The broker rounds each trade's cash to cents (Alpaca: in its favour), so its cash moves a few cents more
        # than the fills say. That gap would land in unassigned cash; charge it to the portfolios that traded.
        # ponytail: a gap over 2 cents per fill is taken as something else (a deposit) and stays unassigned.
        gap = (snap.cash - assigned) - loose_before
        if gap and abs(gap) <= D("0.02") * len(traded):
            with conn:
                for pid in sorted(set(traded)):
                    pf.change_cash(conn, pid, "ROUNDING", gap * traded.count(pid) / len(traded),
                                   note="broker cash rounding")
            assigned = pf.assigned_cash(conn, account["id"])
    if assigned > snap.cash and not awaiting_fills:
        mismatches.append({"kind": "CASH", "appValue": float(assigned), "brokerValue": float(snap.cash),
                           "detail": "portfolios hold more cash than the broker account"})
    unassigned = {"cash": float(max(snap.cash - assigned, D0)), "shares": unassigned_shares}
    if mismatches and mismatches != (previous or {}).get("mismatches"):  # log when they appear or change
        audit.record(conn, "BROKER_MISMATCH", "BROKER_ACCOUNT", account["id"], source="SYNC",
                     details={"mismatches": mismatches}, outcome="FAILED", error=f"{len(mismatches)} difference(s)")
    if previous and previous.get("mismatches") and not mismatches:
        audit.record(conn, "BROKER_MISMATCH_RESOLVED", "BROKER_ACCOUNT", account["id"], source="SYNC")
    return store.add_sync(conn, account["id"], orders_updated, fills_imported, mismatches, None,
                          external_fills=external, unassigned=unassigned)


def run_account_sync(conn, broker_factory, account: dict) -> dict:
    """sync_account with the account's status kept up to date and failures recorded.

    Args:
        conn: app DB connection.
        broker_factory: creates the Broker for the account (ctx.broker_factory).
        account: the broker account dict.

    Returns:
        The sync record.

    Raises:
        BrokerError: the broker failed (status ERROR, failed sync stored).

    Used by: automations.scheduler.sync_brokers, automations.runner.Trading, portfolios.routes.run_sync.
    """
    from ..brokers import store as brokers_store

    try:
        result = sync_account(conn, broker_factory(account), account)
    except BrokerError as e:
        brokers_store.set_status(conn, account["id"], "ERROR", str(e))
        store.add_sync(conn, account["id"], 0, 0, [], str(e))
        audit.record(conn, "BROKER_SYNC_FAILED", "BROKER_ACCOUNT", account["id"], source="SYNC", outcome="FAILED",
                     error=str(e))
        raise
    brokers_store.set_status(conn, account["id"], "CONNECTED", synced=True)
    return result
