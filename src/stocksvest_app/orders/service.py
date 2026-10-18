"""Placing and cancelling orders, and applying what the broker reports (order updates and fills)
to orders, position lots and budget pools."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..audit import store as audit
from ..automations import budget
from ..brokers.base import (
    FINAL_STATUSES,
    REJECTED,
    Broker,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderRequest,
)
from ..db import iso, new_id, now_iso
from ..portfolios import store as pf
from ..portfolios.store import D, D0
from . import store

# Market and stop buys can fill above the last price; reserve a little extra and release the rest later.
PRICE_BUFFER = Decimal("0.01")
# A PENDING order the broker has never heard of after this long is treated as rejected.
PENDING_TIMEOUT = timedelta(minutes=5)


class OrderRejected(ValueError):
    """The app refused the order before sending it (`reason` is a SkipReason-style code)."""

    def __init__(self, message: str, reason: str = "INVALID_ORDER"):
        super().__init__(message)
        self.reason = reason


def check_request(broker: Broker, *, type: str, time_in_force: str, quantity: Decimal, limit_price, stop_price,
                  trail_percent, extended_hours: bool) -> None:
    caps = broker.capabilities()
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise OrderRejected("quantity must be a whole number of shares above 0", "ZERO_QUANTITY")
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
    """The price used to reserve money for a buy."""
    if type in ("LIMIT", "STOP_LIMIT"):
        return D(limit_price)
    base = stop_price if type == "STOP" and stop_price is not None else estimated_price
    return D(base) * (1 + PRICE_BUFFER) if base is not None else None


def open_manual_buys(conn, portfolio_id: str) -> Decimal:
    """Money tied up by open manual buy orders (automation buys are covered by their pools)."""
    rows = conn.execute("SELECT * FROM orders WHERE portfolio_id = ? AND automation_id IS NULL AND side = 'BUY' "
                        "AND status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')", (portfolio_id,)).fetchall()
    total = D0
    for r in rows:
        price = cost_basis_price(r["type"], r["limit_price"], r["stop_price"], r["estimated_price"]) or D0
        total += (D(r["quantity"]) - D(r["filled_quantity"])) * price
    return total


def open_sell_quantity(conn, portfolio_id: str, symbol: str, automation_id: str | None) -> Decimal:
    rows = conn.execute("SELECT quantity, filled_quantity FROM orders WHERE portfolio_id = ? AND symbol = ? "
                        "AND automation_id IS ? AND side = 'SELL' AND status IN ('PENDING', 'SUBMITTED', "
                        "'PARTIALLY_FILLED')", (portfolio_id, symbol, automation_id)).fetchall()
    return sum((D(r["quantity"]) - D(r["filled_quantity"]) for r in rows), D0)


def place_order(conn: sqlite3.Connection, broker: Broker, portfolio: dict, *, symbol: str, side: str, type: str,
                quantity: Decimal, time_in_force: str, limit_price=None, stop_price=None, trail_percent=None,
                extended_hours: bool = False, automation_id: str | None = None, run_id: str | None = None,
                client_order_id: str | None = None, estimated_price=None) -> dict:
    """Check, record, reserve budget, and send to the broker. Raises OrderRejected for app-side refusals;
    broker refusals are recorded as REJECTED orders."""
    check_request(broker, type=type, time_in_force=time_in_force, quantity=quantity, limit_price=limit_price,
                  stop_price=stop_price, trail_percent=trail_percent, extended_hours=extended_hours)
    if client_order_id and store.get_order_row(conn, client_order_id=client_order_id):
        return store.get_order(conn, store.get_order_row(conn, client_order_id=client_order_id)["id"])  # idempotent

    reserve_amount = D0
    if side == "SELL":
        free = pf.held(conn, portfolio["id"], symbol, automation_id) - open_sell_quantity(conn, portfolio["id"], symbol,
                                                                                        automation_id)
        if quantity > free:
            raise OrderRejected(f"only {free} {symbol} shares are available to sell"
                                f"{' for this automation' if automation_id else ''}", "NOT_HELD")
    else:
        price = cost_basis_price(type, limit_price, stop_price, estimated_price)
        if price is None:
            raise OrderRejected("no price to estimate the cost of this buy", "NO_PRICE")
        reserve_amount = (quantity * price).quantize(Decimal("0.01"))
        if automation_id:
            if reserve_amount > budget.available(conn, automation_id):
                raise OrderRejected(f"needs {reserve_amount}, budget has {budget.available(conn, automation_id):.2f}",
                                    "INSUFFICIENT_BUDGET")
        elif reserve_amount > budget.unallocated_cash(conn, portfolio, open_manual_buys(conn, portfolio["id"])):
            raise OrderRejected(f"needs {reserve_amount}; not enough unallocated cash", "INSUFFICIENT_BUDGET")

    client_order_id = client_order_id or new_id("sv")
    with conn:
        order_id = store.insert_order(
            conn, portfolio_id=portfolio["id"], broker_account_id=portfolio["brokerAccountId"], symbol=symbol,
            automation_id=automation_id, run_id=run_id, client_order_id=client_order_id, side=side, type=type,
            time_in_force=time_in_force, quantity=quantity, limit_price=limit_price, stop_price=stop_price,
            trail_percent=trail_percent, extended_hours=extended_hours, estimated_price=estimated_price,
            reserved=reserve_amount if automation_id else D0)
        if automation_id and reserve_amount:
            budget.reserve(conn, automation_id, reserve_amount, order_id, run_id)

    request = BrokerOrderRequest(client_order_id=client_order_id, symbol=symbol, side=side, type=type,
                                 quantity=quantity, time_in_force=time_in_force,
                                 limit_price=D(limit_price) if limit_price is not None else None,
                                 stop_price=D(stop_price) if stop_price is not None else None,
                                 trail_percent=D(trail_percent) if trail_percent is not None else None,
                                 extended_hours=extended_hours)
    related = {"portfolioId": portfolio["id"], "automationId": automation_id, "runId": run_id}
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
    """Copy the broker's view of an order. Returns True if anything changed."""
    changed = (bo.status != row["status"] or str(bo.filled_quantity) != row["filled_quantity"]
               or bo.broker_order_id != row["broker_order_id"])
    if log and bo.status != row["status"]:
        audit.record(conn, f"ORDER_{bo.status}", "ORDER", row["id"], source="SYNC",
                     related={"portfolioId": row["portfolio_id"], "automationId": row["automation_id"],
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
    """An order reached a final status: stamp the time and give unused reserved budget back."""
    ts = now_iso()
    if row["status"] == "FILLED":
        store.update_order(conn, row["id"], filled_at=ts)
    else:
        store.update_order(conn, row["id"], cancelled_at=ts)
    left = D(row["reserved"])
    if row["automation_id"] and left > 0:
        budget.release(conn, row["automation_id"], left, row["id"], f"order {row['status'].lower()}")
        store.update_order(conn, row["id"], reserved=D0)


def apply_fill(conn, row: sqlite3.Row, fill: BrokerFill) -> list[dict]:
    """Record a fill of an app order and update lots, the portfolio's cash and the owning automation's budget.
    Returns mismatches, if any."""
    portfolio_id, owner, order_id = row["portfolio_id"], row["automation_id"], row["id"]
    fill_id = store.insert_fill(conn, order_id, portfolio_id, fill)
    audit.record(conn, "FILL_IMPORTED", "ORDER", order_id, source="SYNC",
                 related={"portfolioId": portfolio_id, "automationId": owner, "fillId": fill_id},
                 details={"symbol": fill.symbol, "side": fill.side, "quantity": str(fill.quantity),
                          "price": str(fill.price), "fee": str(fill.fee), "brokerFillId": fill.broker_fill_id})
    cost = fill.quantity * fill.price
    mismatches = []
    if fill.side == "BUY":
        pf.change_cash(conn, portfolio_id, "BUY_FILL", -cost, automation_id=owner, order_id=order_id, fill_id=fill_id)
    else:
        pf.change_cash(conn, portfolio_id, "SELL_FILL", cost, automation_id=owner, order_id=order_id, fill_id=fill_id)
    if fill.fee:
        pf.change_cash(conn, portfolio_id, "FEE", -fill.fee, automation_id=owner, order_id=order_id, fill_id=fill_id)
    if fill.side == "BUY":
        pf.open_lot(conn, portfolio_id, fill.symbol, owner, fill.quantity, fill.price, buy_fill_id=fill_id,
                    opened_at=iso(fill.executed_at))
        if owner:
            used = min(D(row["reserved"]), cost)
            budget.buy_fill(conn, owner, cost, used, row["id"], fill_id)
            store.update_order(conn, row["id"], reserved=D(row["reserved"]) - used)
    else:
        taken, basis, pnl = pf.close_lots_fifo(conn, portfolio_id, fill.symbol, owner, fill.quantity, fill.price)
        if owner:
            budget.sell_fill(conn, owner, taken * fill.price, basis, pnl, row["id"], fill_id)
        if taken < fill.quantity:
            # Sold more than the owner held: take it from the portfolio's manual shares.
            extra = D0
            if owner:
                extra, _, _ = pf.close_lots_fifo(conn, portfolio_id, fill.symbol, None, fill.quantity - taken, fill.price)
            if taken + extra < fill.quantity:
                mismatches.append({"kind": "POSITION_QUANTITY", "symbol": fill.symbol,
                                   "detail": f"sell fill of {fill.quantity} but only {taken + extra} shares were held"})
    if owner and fill.fee:
        budget.fee(conn, owner, fill.fee, row["id"], fill_id)
    return mismatches


def cancel_order(conn, broker: Broker, order_id: str) -> dict:
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
    orders; fills of orders placed outside the app are recorded as external), order updates, then compare
    the app's positions and cash with the broker. Shares and cash not in any portfolio are "unassigned"."""
    from ..brokers import store as brokers_store

    snap = broker.account_snapshot()
    brokers_store.set_balances(conn, account["id"], snap.cash, snap.buying_power, snap.equity)
    portfolios = pf.list_portfolios(conn, account["id"])
    previous = store.last_sync(conn, account["id"])

    # Fills first, so final orders release only what their fills did not use.
    since_str = store.last_fill_time(conn, account["id"]) or account["createdAt"]
    since = datetime.fromisoformat(since_str) - timedelta(days=1)
    fills_imported = external = 0
    mismatches = []
    for f in broker.list_fills(since):
        if store.fill_exists(conn, f.broker_fill_id):
            continue
        with conn:
            row = store.get_order_row(conn, broker_order_id=f.broker_order_id)
            if row is None:
                store.insert_external_fill(conn, account["id"], f)
                audit.record(conn, "EXTERNAL_FILL", "BROKER_ACCOUNT", account["id"], source="SYNC",
                             details={"symbol": f.symbol, "side": f.side, "quantity": str(f.quantity),
                                      "price": str(f.price), "brokerFillId": f.broker_fill_id})
                external += 1
                continue
            mismatches += apply_fill(conn, row, f)
        fills_imported += 1

    orders_updated = 0
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
                orders_updated += apply_order_update(conn, row, bo)

    # Compare: the broker holds the app's shares plus unassigned ones; it can never hold fewer.
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
        elif a > b:
            mismatches.append({"kind": "POSITION_QUANTITY", "symbol": symbol, "appValue": float(a),
                               "brokerValue": float(b), "detail": "portfolios hold more shares than the broker"})
    assigned = pf.assigned_cash(conn, account["id"])
    if assigned > snap.cash:
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
    """sync_account with the account's status kept up to date. Raises BrokerError when the broker fails."""
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
