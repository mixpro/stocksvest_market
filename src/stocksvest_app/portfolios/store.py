"""Portfolios, positions and position lots (UML/InvestmentPortfolio.mmd).

Money and quantities are stored as decimal strings. Each buy fill opens a lot tagged with the
automation that bought it (NULL = manual); sells close lots FIFO within the same owner, so
positions, P&L and budgets stay per automation.
"""

import sqlite3
from decimal import Decimal

from ..db import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    broker_account_id TEXT NOT NULL UNIQUE REFERENCES broker_accounts(id),
    currency          TEXT NOT NULL,
    cash              TEXT NOT NULL DEFAULT '0',   -- from the broker (source of truth)
    buying_power      TEXT NOT NULL DEFAULT '0',
    equity            TEXT NOT NULL DEFAULT '0',
    cash_synced_at    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_lots (
    id                 TEXT PRIMARY KEY,
    portfolio_id       TEXT NOT NULL REFERENCES portfolios(id),
    symbol             TEXT NOT NULL,
    automation_id      TEXT,                     -- NULL = manual / opening balance
    source             TEXT NOT NULL,            -- FILL | OPENING
    buy_fill_id        TEXT,
    quantity           TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL,
    cost_per_share     TEXT NOT NULL,
    realized_pnl       TEXT NOT NULL DEFAULT '0',
    opened_at          TEXT NOT NULL,
    closed_at          TEXT
);
CREATE INDEX IF NOT EXISTS lots_open ON position_lots(portfolio_id, symbol, automation_id, closed_at);
"""

D0 = Decimal(0)


def D(v) -> Decimal:
    return Decimal(str(v)) if v is not None else D0


def num(v) -> float | None:
    """Decimal (or decimal string) -> JSON number."""
    return float(v) if v is not None else None


# ------------------------------------------------------------------ portfolios


def portfolio_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "description": r["description"],
            "brokerAccountId": r["broker_account_id"], "currency": r["currency"], "cash": num(r["cash"]),
            "buyingPower": num(r["buying_power"]), "equity": num(r["equity"]), "cashSyncedAt": r["cash_synced_at"],
            "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


def get_portfolio(conn, portfolio_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    return portfolio_json(r) if r else None


def list_portfolios(conn) -> list[dict]:
    return [portfolio_json(r) for r in conn.execute("SELECT * FROM portfolios ORDER BY name")]


def create_portfolio(conn, name: str, description: str, account: dict) -> dict:
    pid, ts = new_id("pf"), now_iso()
    with conn:
        conn.execute(
            "INSERT INTO portfolios(id, name, description, broker_account_id, currency, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, name, description, account["id"], account["baseCurrency"], ts, ts),
        )
    return get_portfolio(conn, pid)


def update_portfolio(conn, portfolio_id: str, name: str, description: str) -> dict:
    with conn:
        conn.execute("UPDATE portfolios SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                     (name, description, now_iso(), portfolio_id))
    return get_portfolio(conn, portfolio_id)


def set_balances(conn, portfolio_id: str, cash: Decimal, buying_power: Decimal, equity: Decimal) -> None:
    ts = now_iso()
    with conn:
        conn.execute("UPDATE portfolios SET cash = ?, buying_power = ?, equity = ?, cash_synced_at = ?, updated_at = ? "
                     "WHERE id = ?", (str(cash), str(buying_power), str(equity), ts, ts, portfolio_id))


# ------------------------------------------------------------------ lots and positions


def lot_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "symbol": r["symbol"], "automationId": r["automation_id"], "source": r["source"],
            "quantity": num(r["quantity"]), "remainingQuantity": num(r["remaining_quantity"]),
            "costPerShare": num(r["cost_per_share"]), "realizedPnL": num(r["realized_pnl"]),
            "openedAt": r["opened_at"], "closedAt": r["closed_at"]}


def open_lot(conn, portfolio_id: str, symbol: str, automation_id: str | None, quantity: Decimal,
             cost_per_share: Decimal, source: str = "FILL", buy_fill_id: str | None = None,
             opened_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO position_lots(id, portfolio_id, symbol, automation_id, source, buy_fill_id, quantity, "
        "remaining_quantity, cost_per_share, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("lot"), portfolio_id, symbol, automation_id, source, buy_fill_id, str(quantity), str(quantity),
         str(cost_per_share), opened_at or now_iso()),
    )


def open_lots(conn, portfolio_id: str, symbol: str | None = None, automation_id: str | None = None,
              any_owner: bool = False) -> list[sqlite3.Row]:
    """Open lots, oldest first. By default only one owner: the automation, or manual when automation_id is None."""
    sql, args = "SELECT * FROM position_lots WHERE portfolio_id = ? AND closed_at IS NULL", [portfolio_id]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    if not any_owner:
        sql += " AND automation_id IS ?"
        args.append(automation_id)
    return conn.execute(sql + " ORDER BY opened_at, rowid", args).fetchall()


def held(conn, portfolio_id: str, symbol: str, automation_id: str | None = None, any_owner: bool = False) -> Decimal:
    return sum((D(r["remaining_quantity"]) for r in open_lots(conn, portfolio_id, symbol, automation_id, any_owner)), D0)


def close_lots_fifo(conn, portfolio_id: str, symbol: str, automation_id: str | None, quantity: Decimal,
                    price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Sell `quantity` from the owner's lots, oldest first.
    Returns (quantity actually taken from lots, their cost basis, realized P&L)."""
    left, basis, pnl = quantity, D0, D0
    for lot in open_lots(conn, portfolio_id, symbol, automation_id):
        if left <= 0:
            break
        remaining, cost = D(lot["remaining_quantity"]), D(lot["cost_per_share"])
        take = min(left, remaining)
        lot_pnl = (price - cost) * take
        conn.execute(
            "UPDATE position_lots SET remaining_quantity = ?, realized_pnl = ?, closed_at = ? WHERE id = ?",
            (str(remaining - take), str(D(lot["realized_pnl"]) + lot_pnl),
             now_iso() if remaining - take == 0 else None, lot["id"]),
        )
        basis += cost * take
        pnl += lot_pnl
        left -= take
    return quantity - left, basis, pnl


def positions(conn, portfolio_id: str, automation_id: str | None = None, by_owner: bool = False) -> list[dict]:
    """Current positions from open lots: quantity, average cost and cost basis per symbol
    (per owner too with by_owner). `automation_id` limits to one automation's shares."""
    sql = "SELECT * FROM position_lots WHERE portfolio_id = ? AND closed_at IS NULL"
    args = [portfolio_id]
    if automation_id:
        sql += " AND automation_id = ?"
        args.append(automation_id)
    out: dict[tuple, dict] = {}
    for r in conn.execute(sql + " ORDER BY symbol", args):
        key = (r["symbol"], r["automation_id"]) if by_owner else (r["symbol"],)
        p = out.setdefault(key, {"symbol": r["symbol"], "quantity": D0, "costBasis": D0,
                                 **({"automationId": r["automation_id"]} if by_owner else {})})
        qty = D(r["remaining_quantity"])
        p["quantity"] += qty
        p["costBasis"] += qty * D(r["cost_per_share"])
    rows = []
    for p in out.values():
        p["averageCost"] = p["costBasis"] / p["quantity"] if p["quantity"] else D0
        rows.append(p)
    return rows


def realized_pnl(conn, portfolio_id: str, automation_id: str | None = None) -> Decimal:
    sql, args = "SELECT realized_pnl FROM position_lots WHERE portfolio_id = ?", [portfolio_id]
    if automation_id:
        sql += " AND automation_id = ?"
        args.append(automation_id)
    return sum((D(r["realized_pnl"]) for r in conn.execute(sql, args)), D0)
