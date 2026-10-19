"""Portfolios, their capital, positions and position lots (UML/InvestmentPortfolio.mmd).

A broker account can hold several portfolios. Each portfolio has its own capital, carved out of the
account's cash (the rest of the account is "unassigned"), and a ledger of every cash change. Budgets
(budgets.store) are carved out of the portfolio's cash in turn and shared by the automations attached to them.

Money and quantities are stored as decimal strings. Each buy fill opens a lot tagged with the portfolio, the budget
that paid (its owner; NULL = manual) and the automation that bought it (shown only). Sells close lots FIFO within
the same budget, whoever bought them, so positions, P&L and money stay per portfolio and per budget.
"""

import sqlite3
from decimal import Decimal

from ..db import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    broker_account_id TEXT NOT NULL REFERENCES broker_accounts(id),
    currency          TEXT NOT NULL,
    allocated         TEXT NOT NULL DEFAULT '0',   -- capital put in minus capital taken out
    cash              TEXT NOT NULL DEFAULT '0',   -- uninvested money (includes its budgets' cash)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Every change of a portfolio's cash. Kept permanently.
CREATE TABLE IF NOT EXISTS portfolio_entries (
    id            TEXT PRIMARY KEY,
    portfolio_id  TEXT NOT NULL,
    type          TEXT NOT NULL,             -- ALLOCATE WITHDRAW BUY_FILL SELL_FILL FEE ADJUSTMENT
    amount        TEXT NOT NULL,             -- signed change of cash
    cash_after    TEXT NOT NULL,
    automation_id TEXT,
    order_id      TEXT,
    fill_id       TEXT,
    note          TEXT,
    created_at    TEXT NOT NULL,
    budget_id     TEXT
);
CREATE INDEX IF NOT EXISTS portfolio_entries_by_portfolio ON portfolio_entries(portfolio_id, created_at);

CREATE TABLE IF NOT EXISTS position_lots (
    id                 TEXT PRIMARY KEY,
    portfolio_id       TEXT NOT NULL REFERENCES portfolios(id),
    symbol             TEXT NOT NULL,
    automation_id      TEXT,                     -- who bought them (NULL = manual / opening balance)
    source             TEXT NOT NULL,            -- FILL | OPENING
    buy_fill_id        TEXT,
    quantity           TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL,
    cost_per_share     TEXT NOT NULL,
    realized_pnl       TEXT NOT NULL DEFAULT '0',
    opened_at          TEXT NOT NULL,
    closed_at          TEXT,
    budget_id          TEXT                      -- owner: the budget that paid (NULL = the portfolio's manual shares)
);
CREATE INDEX IF NOT EXISTS lots_open ON position_lots(portfolio_id, symbol, automation_id, closed_at);
"""

D0 = Decimal(0)  # Used by: this module, budgets.store, orders.service, performance.snapshots, portfolios.routes


def D(v) -> Decimal:
    """Any number or decimal string as Decimal (None -> 0).

    Args:
        v: the value.

    Used by: this module, budgets.store, orders.service, performance.snapshots.
    """
    return Decimal(str(v)) if v is not None else D0


def num(v) -> float | None:
    """Decimal (or decimal string) -> JSON number.

    Args:
        v: the value; None stays None.

    Used by: the *_json helpers here and in budgets.store and orders.store, portfolios.routes.summary.
    """
    return float(v) if v is not None else None


# ------------------------------------------------------------------ portfolios


def portfolio_json(r: sqlite3.Row) -> dict:
    """A portfolios row in API form.

    Args:
        r: the row.

    Used by: get_portfolio, list_portfolios.
    """
    return {"id": r["id"], "name": r["name"], "description": r["description"],
            "brokerAccountId": r["broker_account_id"], "currency": r["currency"], "allocated": num(r["allocated"]),
            "cash": num(r["cash"]), "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


def get_portfolio(conn, portfolio_id: str) -> dict | None:
    """One portfolio, or None.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.

    Used by: portfolios.routes, automations.routes, automations.runner.Trading, performance.routes.
    """
    r = conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    return portfolio_json(r) if r else None


def list_portfolios(conn, broker_account_id: str | None = None) -> list[dict]:
    """Portfolios by name.

    Args:
        conn: app DB connection.
        broker_account_id: only this broker account's (default all).

    Used by: portfolios.routes, brokers.routes.overview, orders.service.sync_account, performance.snapshots.scheduled.
    """
    if broker_account_id:
        rows = conn.execute("SELECT * FROM portfolios WHERE broker_account_id = ? ORDER BY name", (broker_account_id,))
    else:
        rows = conn.execute("SELECT * FROM portfolios ORDER BY name")
    return [portfolio_json(r) for r in rows]


def create_portfolio(conn, name: str, description: str, account: dict, capital: Decimal) -> dict:
    """Store a new portfolio with its initial capital. Call inside a transaction (the caller checks the account's unassigned cash under the same lock).

    Args:
        conn: app DB connection.
        name: display name.
        description: description.
        account: its broker account dict.
        capital: initial capital (an ALLOCATE ledger entry when above 0).

    Returns:
        The stored portfolio.

    Used by: portfolios.routes.create_portfolio.
    """
    pid, ts = new_id("pf"), now_iso()
    conn.execute(
        "INSERT INTO portfolios(id, name, description, broker_account_id, currency, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, name, description, account["id"], account["baseCurrency"], ts, ts),
    )
    if capital > 0:
        change_cash(conn, pid, "ALLOCATE", capital, allocated=capital, note="initial capital")
    return get_portfolio(conn, pid)


def update_portfolio(conn, portfolio_id: str, name: str, description: str) -> dict:
    """Rename / redescribe a portfolio.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        name: new name.
        description: new description.

    Returns:
        The portfolio.

    Used by: portfolios.routes.update_portfolio.
    """
    with conn:
        conn.execute("UPDATE portfolios SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                     (name, description, now_iso(), portfolio_id))
    return get_portfolio(conn, portfolio_id)


def delete_portfolio(conn, portfolio_id: str) -> None:
    """Delete a portfolio and its ledger (the routes check it is empty first).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.

    Used by: portfolios.routes.delete_portfolio.
    """
    with conn:
        conn.execute("DELETE FROM portfolio_entries WHERE portfolio_id = ?", (portfolio_id,))
        conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))


# ------------------------------------------------------------------ capital ledger


def assigned_cash(conn, broker_account_id: str) -> Decimal:
    """Cash of all portfolios on a broker account.

    Args:
        conn: app DB connection.
        broker_account_id: the account's id.

    Used by: orders.service.sync_account, portfolios.routes.unassigned_cash.
    """
    rows = conn.execute("SELECT cash FROM portfolios WHERE broker_account_id = ?", (broker_account_id,))
    return sum((D(r["cash"]) for r in rows), D0)


def change_cash(conn, portfolio_id: str, type_: str, amount: Decimal, *, allocated: Decimal = D0,
                automation_id=None, order_id=None, fill_id=None, note=None, budget_id=None) -> None:
    """Apply a signed cash change and record it in the ledger. Call inside a transaction.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        type_: entry type: ALLOCATE, WITHDRAW, BUY_FILL, SELL_FILL or FEE.
        amount: signed change of cash.
        allocated: signed change of the allocated capital (ALLOCATE / WITHDRAW).
        automation_id: the automation it concerns.
        order_id: the order it concerns.
        fill_id: the fill it concerns.
        note: free text.
        budget_id: the budget it concerns.

    Used by: create_portfolio, orders.service.apply_fill, portfolios.routes.allocate / withdraw.
    """
    r = conn.execute("SELECT cash, allocated FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    cash = D(r["cash"]) + amount
    conn.execute("UPDATE portfolios SET cash = ?, allocated = ?, updated_at = ? WHERE id = ?",
                 (str(cash), str(D(r["allocated"]) + allocated), now_iso(), portfolio_id))
    conn.execute(
        "INSERT INTO portfolio_entries(id, portfolio_id, type, amount, cash_after, automation_id, budget_id, order_id, "
        "fill_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("pfe"), portfolio_id, type_, str(amount), str(cash), automation_id, budget_id, order_id, fill_id, note,
         now_iso()),
    )


def entries(conn, portfolio_id: str, limit: int = 200) -> list[dict]:
    """A portfolio's ledger, newest first.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        limit: how many.

    Used by: portfolios.routes.ledger.
    """
    rows = conn.execute("SELECT * FROM portfolio_entries WHERE portfolio_id = ? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT ?", (portfolio_id, limit))
    return [{"id": r["id"], "type": r["type"], "amount": num(r["amount"]), "cashAfter": num(r["cash_after"]),
             "automationId": r["automation_id"], "budgetId": r["budget_id"], "orderId": r["order_id"],
             "fillId": r["fill_id"], "note": r["note"],
             "createdAt": r["created_at"]} for r in rows]


# ------------------------------------------------------------------ lots and positions


def lot_json(r: sqlite3.Row) -> dict:
    """A position_lots row in API form.

    Args:
        r: the row.

    Used by: portfolios.routes.lots.
    """
    return {"id": r["id"], "symbol": r["symbol"], "budgetId": r["budget_id"], "automationId": r["automation_id"],
            "source": r["source"], "quantity": num(r["quantity"]), "remainingQuantity": num(r["remaining_quantity"]),
            "costPerShare": num(r["cost_per_share"]), "realizedPnL": num(r["realized_pnl"]),
            "openedAt": r["opened_at"], "closedAt": r["closed_at"]}


def open_lot(conn, portfolio_id: str, symbol: str, automation_id: str | None, quantity: Decimal,
             cost_per_share: Decimal, source: str = "FILL", buy_fill_id: str | None = None,
             opened_at: str | None = None, budget_id: str | None = None) -> None:
    """Open a lot for bought shares (inside the caller's transaction).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        symbol: ticker.
        automation_id: the automation that bought them, or None (manual). Shown only; ownership is budget_id.
        quantity: shares.
        cost_per_share: fill price.
        source: FILL, or OPENING for shares present when the app started.
        buy_fill_id: the fill that opened it.
        opened_at: time (default now).
        budget_id: the budget that paid and owns the shares, or None (the portfolio's manual shares).

    Used by: orders.service.apply_fill.
    """
    conn.execute(
        "INSERT INTO position_lots(id, portfolio_id, symbol, automation_id, budget_id, source, buy_fill_id, quantity, "
        "remaining_quantity, cost_per_share, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("lot"), portfolio_id, symbol, automation_id, budget_id, source, buy_fill_id, str(quantity),
         str(quantity), str(cost_per_share), opened_at or now_iso()),
    )


def open_lots(conn, portfolio_id: str, symbol: str | None = None, budget_id: str | None = None,
              any_owner: bool = False) -> list[sqlite3.Row]:
    """Open lots, oldest first. By default only one owner: the budget, or manual shares when budget_id is None.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        symbol: only this ticker.
        budget_id: the owning budget; None = manual shares.
        any_owner: ignore the owner.

    Used by: held, close_lots_fifo.
    """
    sql, args = "SELECT * FROM position_lots WHERE portfolio_id = ? AND closed_at IS NULL", [portfolio_id]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    if not any_owner:
        sql += " AND budget_id IS ?"
        args.append(budget_id)
    return conn.execute(sql + " ORDER BY opened_at, rowid", args).fetchall()


def held(conn, portfolio_id: str, symbol: str, budget_id: str | None = None, any_owner: bool = False) -> Decimal:
    """Shares a portfolio holds of a symbol (one owner unless any_owner).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        symbol: ticker.
        budget_id: the owning budget; None = manual shares.
        any_owner: all owners.

    Used by: orders.service._check_funds.
    """
    return sum((D(r["remaining_quantity"]) for r in open_lots(conn, portfolio_id, symbol, budget_id, any_owner)), D0)


def close_lots_fifo(conn, portfolio_id: str, symbol: str, budget_id: str | None, quantity: Decimal,
                    price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Sell `quantity` from the owner's lots, oldest first, whoever bought them (inside the caller's transaction).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        symbol: ticker.
        budget_id: the owning budget; None = manual shares.
        quantity: shares sold.
        price: sell price.

    Returns:
        (quantity actually taken from lots, their cost basis, realized P&L).

    Used by: orders.service.apply_fill.
    """
    left, basis, pnl = quantity, D0, D0
    for lot in open_lots(conn, portfolio_id, symbol, budget_id):
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


def positions(conn, portfolio_id: str, budget_id: str | None = None, by_owner: bool = False) -> list[dict]:
    """Current positions from open lots: quantity, average cost and cost basis per symbol
    (per budget and buying automation too with by_owner).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        budget_id: only this budget's shares.
        by_owner: one row per symbol, budget and buying automation (adds budgetId, automationId).

    Returns:
        [{symbol, quantity, costBasis, averageCost[, budgetId, automationId]}] (Decimals).

    Used by: portfolios.routes, orders.service.sync_account, automations.runner.Trading.context, performance.snapshots.
    """
    sql = "SELECT * FROM position_lots WHERE portfolio_id = ? AND closed_at IS NULL"
    args = [portfolio_id]
    if budget_id:
        sql += " AND budget_id = ?"
        args.append(budget_id)
    out: dict[tuple, dict] = {}
    for r in conn.execute(sql + " ORDER BY symbol", args):
        key = (r["symbol"], r["budget_id"], r["automation_id"]) if by_owner else (r["symbol"],)
        p = out.setdefault(key, {"symbol": r["symbol"], "quantity": D0, "costBasis": D0,
                                 **({"budgetId": r["budget_id"], "automationId": r["automation_id"]}
                                    if by_owner else {})})
        qty = D(r["remaining_quantity"])
        p["quantity"] += qty
        p["costBasis"] += qty * D(r["cost_per_share"])
    rows = []
    for p in out.values():
        p["averageCost"] = p["costBasis"] / p["quantity"] if p["quantity"] else D0
        rows.append(p)
    return rows


def realized_pnl(conn, portfolio_id: str, budget_id: str | None = None) -> Decimal:
    """Realized P&L of a portfolio's lots (or of one budget's).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        budget_id: only this budget's.

    Used by: portfolios.routes.summary, performance.snapshots.portfolio_values.
    """
    sql, args = "SELECT realized_pnl FROM position_lots WHERE portfolio_id = ?", [portfolio_id]
    if budget_id:
        sql += " AND budget_id = ?"
        args.append(budget_id)
    return sum((D(r["realized_pnl"]) for r in conn.execute(sql, args)), D0)
