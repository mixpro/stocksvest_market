"""Orders, fills and broker sync records (UML/Orders.mmd, UML/Brokers.mmd). Kept permanently."""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from ..db import iso, new_id, now_iso
from ..portfolios.store import num

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id                 TEXT PRIMARY KEY,
    portfolio_id       TEXT NOT NULL REFERENCES portfolios(id),
    broker_account_id  TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    automation_id      TEXT,                       -- the automation that placed it (NULL = manual)
    automation_run_id  TEXT,
    client_order_id    TEXT NOT NULL UNIQUE,
    broker_order_id    TEXT,
    side               TEXT NOT NULL,
    type               TEXT NOT NULL,
    time_in_force      TEXT NOT NULL,
    quantity           TEXT NOT NULL,
    filled_quantity    TEXT NOT NULL DEFAULT '0',
    limit_price        TEXT,
    stop_price         TEXT,
    trail_percent      TEXT,
    extended_hours     INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL,
    raw_broker_status  TEXT,
    reject_reason      TEXT,
    average_fill_price TEXT,
    estimated_price    TEXT,
    reserved           TEXT NOT NULL DEFAULT '0',   -- budget reserved for this buy, still unused
    budget_id          TEXT,                        -- the budget paying / whose shares it sells (NULL = manual)
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    submitted_at       TEXT,
    filled_at          TEXT,
    cancelled_at       TEXT
);
CREATE INDEX IF NOT EXISTS orders_open ON orders(portfolio_id, status);
CREATE INDEX IF NOT EXISTS orders_by_automation ON orders(automation_id, created_at);

CREATE TABLE IF NOT EXISTS fills (
    id             TEXT PRIMARY KEY,
    order_id       TEXT,                       -- NULL = order placed outside the app
    portfolio_id   TEXT NOT NULL,
    broker_fill_id TEXT NOT NULL UNIQUE,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    quantity       TEXT NOT NULL,
    price          TEXT NOT NULL,
    fee            TEXT NOT NULL DEFAULT '0',
    executed_at    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    realized_pnl   TEXT                        -- sells: P&L of the lots it closed
);
CREATE INDEX IF NOT EXISTS fills_by_portfolio ON fills(portfolio_id, executed_at);

-- One row per sync of a broker account (all its portfolios at once).
CREATE TABLE IF NOT EXISTS broker_syncs (
    id                TEXT PRIMARY KEY,
    broker_account_id TEXT NOT NULL,
    ran_at            TEXT NOT NULL,
    orders_updated    INTEGER NOT NULL,
    fills_imported    INTEGER NOT NULL,
    external_fills    INTEGER NOT NULL DEFAULT 0,
    mismatches        TEXT NOT NULL,             -- JSON list: app and broker disagree
    unassigned        TEXT NOT NULL DEFAULT '{}', -- JSON: account cash and shares not in any portfolio
    error             TEXT
);
CREATE INDEX IF NOT EXISTS broker_syncs_by_account ON broker_syncs(broker_account_id, ran_at);

-- Account fees booked apart from the fills (e.g. Alpaca's daily CAT fee), and who they were charged to.
CREATE TABLE IF NOT EXISTS broker_fees (
    broker_fee_id     TEXT PRIMARY KEY,
    broker_account_id TEXT NOT NULL,
    date              TEXT NOT NULL,             -- the trading day it is for
    amount            TEXT NOT NULL,             -- positive = charged
    description       TEXT NOT NULL,
    charged_to        TEXT NOT NULL,             -- JSON: [{portfolioId, budgetId, amount}], the rest is unassigned
    created_at        TEXT NOT NULL
);

-- Fills of orders placed outside the app (e.g. in the broker's own UI). They belong to no portfolio:
-- their shares and cash show up as "unassigned" on the broker account.
CREATE TABLE IF NOT EXISTS external_fills (
    broker_fill_id    TEXT PRIMARY KEY,
    broker_account_id TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    quantity          TEXT NOT NULL,
    price             TEXT NOT NULL,
    executed_at       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
"""


def order_json(r: sqlite3.Row) -> dict:
    """An orders row in API form (numbers as floats, plus slippage = average fill - estimated price).

    Args:
        r: the row.

    Used by: get_order, list_orders.
    """
    est, avg = r["estimated_price"], r["average_fill_price"]
    return {
        "id": r["id"], "portfolioId": r["portfolio_id"], "brokerAccountId": r["broker_account_id"],
        "symbol": r["symbol"], "automationId": r["automation_id"], "automationRunId": r["automation_run_id"],
        "clientOrderId": r["client_order_id"], "brokerOrderId": r["broker_order_id"], "side": r["side"],
        "type": r["type"], "timeInForce": r["time_in_force"], "quantity": num(r["quantity"]),
        "filledQuantity": num(r["filled_quantity"]), "limitPrice": num(r["limit_price"]),
        "stopPrice": num(r["stop_price"]), "trailPercent": num(r["trail_percent"]),
        "extendedHours": bool(r["extended_hours"]), "status": r["status"], "rawBrokerStatus": r["raw_broker_status"],
        "rejectReason": r["reject_reason"], "averageFillPrice": num(avg), "estimatedPrice": num(est),
        "budgetId": r["budget_id"],
        "slippage": num(Decimal(avg) - Decimal(est)) if avg and est else None,
        "createdAt": r["created_at"], "updatedAt": r["updated_at"], "submittedAt": r["submitted_at"],
        "filledAt": r["filled_at"], "cancelledAt": r["cancelled_at"],
    }


def get_order(conn, order_id: str) -> dict | None:
    """One order, or None.

    Args:
        conn: app DB connection.
        order_id: the order's id.

    Used by: orders.routes, orders.service.
    """
    r = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return order_json(r) if r else None


def get_order_row(conn, **where) -> sqlite3.Row | None:
    """The raw orders row matching one column, or None.

    Args:
        conn: app DB connection.
        where: exactly one column=value, e.g. id=..., client_order_id=... or broker_order_id=....

    Used by: orders.service.
    """
    (col, val), = where.items()
    return conn.execute(f"SELECT * FROM orders WHERE {col} = ?", (val,)).fetchone()


def list_orders(conn, portfolio_id: str | None = None, automation_id: str | None = None, status: str | None = None,
                open_only: bool = False, limit: int = 200, budget_id: str | None = None) -> list[dict]:
    """Orders, newest first.

    Args:
        conn: app DB connection.
        portfolio_id: only this portfolio's.
        automation_id: only this automation's.
        status: only this status.
        open_only: only PENDING, SUBMITTED or PARTIALLY_FILLED.
        limit: how many.
        budget_id: only orders paid from / selling for this budget.

    Used by: orders.service.sync_account, automations.runner.Trading.context, portfolios.routes, budgets.routes.
    """
    where, args = [], []
    for col, val in (("portfolio_id", portfolio_id), ("automation_id", automation_id), ("status", status),
                     ("budget_id", budget_id)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if open_only:
        where.append("status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"SELECT * FROM orders {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?", args + [limit])
    return [order_json(r) for r in rows]


def last_sells(conn, budget_id: str) -> dict[str, datetime]:
    """When this budget last sold each symbol: its newest sell order that sold shares or is still open
    (rejected and unfilled cancelled ones don't count).

    Args:
        conn: app DB connection.
        budget_id: the budget.

    Returns:
        symbol -> the sell order's creation time (UTC).

    Used by: automations.runner.Trading.context (rebuyCooldownMinutes).
    """
    rows = conn.execute(
        "SELECT symbol, MAX(created_at) AS at FROM orders WHERE budget_id = ? AND side = 'SELL' "
        "AND (CAST(filled_quantity AS REAL) > 0 OR status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')) "
        "GROUP BY symbol", (budget_id,))
    return {r["symbol"]: datetime.fromisoformat(r["at"].replace("Z", "+00:00")) for r in rows}


def insert_order(conn, **f) -> str:
    """Store a new PENDING order (inside the caller's transaction).

    Args:
        conn: app DB connection.
        f: portfolio_id, broker_account_id, symbol, client_order_id, side, type, time_in_force, quantity; optional automation_id, run_id, budget_id, limit_price, stop_price, trail_percent, extended_hours, estimated_price, reserved.

    Returns:
        The new order id.

    Used by: orders.service.place_order.
    """
    oid, ts = new_id("ord"), now_iso()
    conn.execute(
        "INSERT INTO orders(id, portfolio_id, broker_account_id, symbol, automation_id, automation_run_id, "
        "client_order_id, side, type, time_in_force, quantity, limit_price, stop_price, trail_percent, "
        "extended_hours, status, estimated_price, reserved, budget_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)",
        (oid, f["portfolio_id"], f["broker_account_id"], f["symbol"], f.get("automation_id"), f.get("run_id"),
         f["client_order_id"], f["side"], f["type"], f["time_in_force"], str(f["quantity"]),
         _s(f.get("limit_price")), _s(f.get("stop_price")), _s(f.get("trail_percent")), int(f.get("extended_hours", False)),
         _s(f.get("estimated_price")), str(f.get("reserved", 0)), f.get("budget_id"), ts, ts),
    )
    return oid


def _s(v) -> str | None:
    """A value as text for storage, or None.

    Args:
        v: the value.
    """
    return str(v) if v is not None else None


def update_order(conn, order_id: str, **fields) -> None:
    """Change some fields of an order (inside the caller's transaction); updated_at is set.

    Args:
        conn: app DB connection.
        order_id: the order's id.
        fields: any of broker_order_id, status, raw_status, reject_reason, filled_quantity, average_fill_price, submitted_at, filled_at, cancelled_at, reserved.

    Used by: orders.service.
    """
    cols = {"broker_order_id": "broker_order_id", "status": "status", "raw_status": "raw_broker_status",
            "reject_reason": "reject_reason", "filled_quantity": "filled_quantity",
            "average_fill_price": "average_fill_price", "submitted_at": "submitted_at", "filled_at": "filled_at",
            "cancelled_at": "cancelled_at", "reserved": "reserved"}
    sets, args = ["updated_at = ?"], [now_iso()]
    for k, v in fields.items():
        sets.append(f"{cols[k]} = ?")
        args.append(_s(v) if isinstance(v, Decimal) else v)
    conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", args + [order_id])


def fill_exists(conn, broker_fill_id: str) -> bool:
    """Whether a broker fill was already imported (as an app or external fill).

    Args:
        conn: app DB connection.
        broker_fill_id: the broker's fill id.

    Used by: orders.service.sync_account.
    """
    return (conn.execute("SELECT 1 FROM fills WHERE broker_fill_id = ?", (broker_fill_id,)).fetchone() is not None
            or conn.execute("SELECT 1 FROM external_fills WHERE broker_fill_id = ?", (broker_fill_id,)).fetchone()
            is not None)


def insert_external_fill(conn, account_id: str, f) -> None:
    """Record a fill of an order placed outside the app.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.
        f: the BrokerFill.

    Used by: orders.service.sync_account.
    """
    conn.execute(
        "INSERT INTO external_fills(broker_fill_id, broker_account_id, symbol, side, quantity, price, executed_at, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f.broker_fill_id, account_id, f.symbol, f.side, str(f.quantity), str(f.price), iso(f.executed_at), now_iso()),
    )


def insert_fill(conn, order_id: str | None, portfolio_id: str, f) -> str:
    """Record a fill of an app order.

    Args:
        conn: app DB connection.
        order_id: the app order's id.
        portfolio_id: its portfolio.
        f: the BrokerFill.

    Returns:
        The new fill id.

    Used by: orders.service.apply_fill.
    """
    fid = new_id("fill")
    conn.execute(
        "INSERT INTO fills(id, order_id, portfolio_id, broker_fill_id, symbol, side, quantity, price, fee, executed_at, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fid, order_id, portfolio_id, f.broker_fill_id, f.symbol, f.side, str(f.quantity), str(f.price), str(f.fee),
         iso(f.executed_at), now_iso()),
    )
    return fid


def set_fill_pnl(conn, fill_id: str, pnl) -> None:
    """Record the realized P&L of a sell fill (inside the caller's transaction).

    Args:
        conn: app DB connection.
        fill_id: the fill's id.
        pnl: P&L of the lots it closed.

    Used by: orders.service.apply_fill.
    """
    conn.execute("UPDATE fills SET realized_pnl = ? WHERE id = ?", (str(pnl), fill_id))


def fee_exists(conn, broker_fee_id: str) -> bool:
    """Whether an account fee was already imported.

    Args:
        conn: app DB connection.
        broker_fee_id: the broker's id of the fee.

    Used by: orders.service.sync_account.
    """
    return conn.execute("SELECT 1 FROM broker_fees WHERE broker_fee_id = ?", (broker_fee_id,)).fetchone() is not None


def insert_fee(conn, account_id: str, fee, charged_to: list[dict]) -> None:
    """Record an imported account fee and who it was charged to (inside the caller's transaction).

    Args:
        conn: app DB connection.
        account_id: the broker account's id.
        fee: the BrokerFee.
        charged_to: [{portfolioId, budgetId, amount}].

    Used by: orders.service.apply_account_fee.
    """
    conn.execute("INSERT INTO broker_fees(broker_fee_id, broker_account_id, date, amount, description, charged_to, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (fee.broker_fee_id, account_id, fee.date, str(fee.amount), fee.description, json.dumps(charged_to),
                  now_iso()))


def last_fee_date(conn, account_id: str) -> str | None:
    """The newest imported account fee's date, or None.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.

    Used by: orders.service.sync_account (fees are fetched from a few days before).
    """
    return conn.execute("SELECT MAX(date) FROM broker_fees WHERE broker_account_id = ?", (account_id,)).fetchone()[0]


def last_fill_time(conn, account_id: str) -> str | None:
    """Time of the newest imported fill of a broker account (app or external), or None.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.

    Used by: orders.service.sync_account (fills are fetched from a day before).
    """
    r = conn.execute(
        "SELECT MAX(t) AS t FROM (SELECT MAX(f.executed_at) AS t FROM fills f JOIN portfolios p ON p.id = f.portfolio_id "
        "WHERE p.broker_account_id = ? UNION ALL SELECT MAX(executed_at) FROM external_fills WHERE broker_account_id = ?)",
        (account_id, account_id)).fetchone()
    return r["t"]


def imported_quantity(conn, order_id: str) -> Decimal:
    """Shares of the order's fills the app has imported.

    Used by: service.sync_account (fills the broker reported but not yet listed).
    """
    return sum((Decimal(r[0]) for r in conn.execute("SELECT quantity FROM fills WHERE order_id = ?", (order_id,))),
               Decimal(0))


def list_fills(conn, portfolio_id: str, order_id: str | None = None, limit: int = 200) -> list[dict]:
    """A portfolio's fills, newest first.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        order_id: only this order's.
        limit: how many.

    Used by: orders.routes.get_order, portfolios.routes.fills.
    """
    sql, args = "SELECT * FROM fills WHERE portfolio_id = ?", [portfolio_id]
    if order_id:
        sql += " AND order_id = ?"
        args.append(order_id)
    rows = conn.execute(sql + " ORDER BY executed_at DESC LIMIT ?", args + [limit])
    return [{"id": r["id"], "orderId": r["order_id"], "brokerFillId": r["broker_fill_id"], "symbol": r["symbol"],
             "side": r["side"], "quantity": num(r["quantity"]), "price": num(r["price"]), "fee": num(r["fee"]),
             "realizedPnL": num(r["realized_pnl"]), "executedAt": r["executed_at"]} for r in rows]


def add_sync(conn, account_id: str, orders_updated: int, fills_imported: int, mismatches: list[dict],
             error: str | None, external_fills: int = 0, unassigned: dict | None = None) -> dict:
    """Store the result of a broker sync.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.
        orders_updated: orders whose state changed.
        fills_imported: app fills imported.
        mismatches: where the app and the broker disagree.
        error: failure message, if it failed.
        external_fills: fills of orders placed outside the app.
        unassigned: cash and shares in no portfolio.

    Returns:
        The stored record (last_sync).

    Used by: orders.service.sync_account, run_account_sync.
    """
    with conn:
        conn.execute(
            "INSERT INTO broker_syncs(id, broker_account_id, ran_at, orders_updated, fills_imported, external_fills, "
            "mismatches, unassigned, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("sync"), account_id, now_iso(), orders_updated, fills_imported, external_fills,
             json.dumps(mismatches), json.dumps(unassigned or {}), error),
        )
    return last_sync(conn, account_id)


def last_sync(conn, account_id: str) -> dict | None:
    """The newest sync record of a broker account, or None.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.

    Used by: orders.service.sync_account, brokers.routes.overview, portfolios.routes.summary.
    """
    r = conn.execute("SELECT * FROM broker_syncs WHERE broker_account_id = ? ORDER BY ran_at DESC, rowid DESC LIMIT 1",
                     (account_id,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "ranAt": r["ran_at"], "ordersUpdated": r["orders_updated"],
            "fillsImported": r["fills_imported"], "externalFills": r["external_fills"],
            "mismatches": json.loads(r["mismatches"]), "unassigned": json.loads(r["unassigned"]), "error": r["error"]}
