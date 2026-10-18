"""Budget pools: the money an automation may trade with, carved out of its portfolio's cash.

Every change is a ledger entry with the balances after it, so the history reads like a bank statement.
  cash      uninvested money of the pool (includes the reserved part)
  reserved  set aside for open buy orders
  available cash - reserved (what new buys may use)
  invested  cost basis of the shares the automation holds
"""

import sqlite3
from decimal import Decimal

from ..db import immediate, new_id, now_iso
from ..portfolios.store import D, D0, num

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_pools (
    automation_id TEXT PRIMARY KEY,
    portfolio_id  TEXT NOT NULL,
    currency      TEXT NOT NULL,
    allocated     TEXT NOT NULL DEFAULT '0',   -- money put in minus money taken out
    cash          TEXT NOT NULL DEFAULT '0',
    reserved      TEXT NOT NULL DEFAULT '0',
    invested      TEXT NOT NULL DEFAULT '0',
    realized_pnl  TEXT NOT NULL DEFAULT '0',
    fees          TEXT NOT NULL DEFAULT '0',
    updated_at    TEXT NOT NULL
);

-- Kept permanently (not pruned with the run history).
CREATE TABLE IF NOT EXISTS budget_entries (
    id             TEXT PRIMARY KEY,
    automation_id  TEXT NOT NULL,
    type           TEXT NOT NULL,              -- ALLOCATE WITHDRAW RESERVE RELEASE BUY_FILL SELL_FILL FEE ADJUSTMENT
    amount         TEXT NOT NULL,              -- signed change of cash (RESERVE/RELEASE: change of reserved)
    cash_after     TEXT NOT NULL,
    reserved_after TEXT NOT NULL,
    run_id         TEXT,
    order_id       TEXT,
    fill_id        TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS budget_entries_by_automation ON budget_entries(automation_id, created_at);
"""


class BudgetError(ValueError):
    """A budget change that isn't possible (not enough money, nothing to withdraw, money left in another portfolio).

    Used by: allocate / withdraw / ensure_pool / close_pool (raised); automations.routes (-> 422).
    """
    pass


def _row(conn, automation_id: str) -> sqlite3.Row | None:
    """The budget_pools row of an automation, or None.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
    """
    return conn.execute("SELECT * FROM budget_pools WHERE automation_id = ?", (automation_id,)).fetchone()


def pool_json(r: sqlite3.Row | None) -> dict | None:
    """A budget_pools row -> API shape {automationId, portfolioId, currency, allocated, cash, reserved, available,
    invested, realizedPnL, fees, updatedAt}; None stays None.

    Args:
        r: the row or None.

    Used by: get_pool, portfolios.routes.summary.
    """
    if r is None:
        return None
    cash, reserved = D(r["cash"]), D(r["reserved"])
    return {"automationId": r["automation_id"], "portfolioId": r["portfolio_id"], "currency": r["currency"],
            "allocated": num(r["allocated"]), "cash": num(cash), "reserved": num(reserved),
            "available": num(cash - reserved), "invested": num(r["invested"]), "realizedPnL": num(r["realized_pnl"]),
            "fees": num(r["fees"]), "updatedAt": r["updated_at"]}


def get_pool(conn, automation_id: str) -> dict | None:
    """An automation's budget, or None if it never had one.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.

    Used by: routes (budget endpoints, delete/move guards), runner.Trading, portfolios.routes.
    """
    return pool_json(_row(conn, automation_id))


def available(conn, automation_id: str) -> Decimal:
    """cash - reserved: what new buys can use (0 without a budget).

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.

    Used by: orders.service._check_funds, withdraw.
    """
    r = _row(conn, automation_id)
    return D(r["cash"]) - D(r["reserved"]) if r else D0


def ensure_pool(conn, automation_id: str, portfolio: dict) -> None:
    """Create the automation's pool in this portfolio. An empty pool left in another portfolio is moved.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        portfolio: the automation's portfolio.

    Raises:
        BudgetError: the pool still holds money or shares in another portfolio.

    Used by: allocate.
    """
    r = _row(conn, automation_id)
    if r and r["portfolio_id"] != portfolio["id"]:
        if D(r["cash"]) or D(r["invested"]) or D(r["reserved"]):
            raise BudgetError("the budget still holds money or shares in another portfolio; withdraw it first")
        conn.execute("DELETE FROM budget_pools WHERE automation_id = ?", (automation_id,))
        r = None
    if r is None:
        conn.execute("INSERT INTO budget_pools(automation_id, portfolio_id, currency, updated_at) VALUES (?, ?, ?, ?)",
                     (automation_id, portfolio["id"], portfolio["currency"], now_iso()))


def unallocated_cash(conn, portfolio: dict, open_manual_buys: Decimal | None = None) -> Decimal:
    """Portfolio cash not given to any automation pool and not reserved by manual buy orders.
    Reads the current cash from the DB (not the possibly stale `portfolio` dict).

    Args:
        conn: app DB connection.
        portfolio: the portfolio (its id is used).
        open_manual_buys: money held by open manual buys; None = compute it now.

    Used by: allocate, orders.service._check_funds (manual buys), portfolios routes (withdraw, summary),
    performance.snapshots.
    """
    if open_manual_buys is None:
        from ..orders.service import open_manual_buys as manual_buys
        open_manual_buys = manual_buys(conn, portfolio["id"])
    cash = conn.execute("SELECT cash FROM portfolios WHERE id = ?", (portfolio["id"],)).fetchone()
    pools = conn.execute("SELECT cash FROM budget_pools WHERE portfolio_id = ?", (portfolio["id"],)).fetchall()
    return D(cash["cash"] if cash else 0) - sum((D(p["cash"]) for p in pools), D0) - open_manual_buys


def _apply(conn, automation_id: str, type_: str, amount: Decimal, *, cash: Decimal = D0, reserved: Decimal = D0,
           invested: Decimal = D0, pnl: Decimal = D0, fees: Decimal = D0, allocated: Decimal = D0,
           run_id=None, order_id=None, fill_id=None, note=None) -> dict:
    """Change a pool's balances and append a ledger entry with the balances after it.

    Args:
        conn: app DB connection (caller manages the transaction).
        automation_id: the automation's id.
        type_: ALLOCATE, WITHDRAW, RESERVE, RELEASE, BUY_FILL, SELL_FILL, FEE or ADJUSTMENT.
        amount: the entry's signed amount (change of cash; for RESERVE/RELEASE, change of reserved).
        cash: change of cash.
        reserved: change of reserved.
        invested: change of invested.
        pnl: change of realized P&L.
        fees: change of fees.
        allocated: change of allocated.
        run_id: related automation run.
        order_id: related order.
        fill_id: related fill.
        note: free text.

    Returns:
        The pool after the change.

    Raises:
        BudgetError: the automation has no pool.
    """
    r = _row(conn, automation_id)
    if r is None:
        raise BudgetError("this automation has no budget")
    new = {"cash": D(r["cash"]) + cash, "reserved": D(r["reserved"]) + reserved,
           "invested": D(r["invested"]) + invested, "realized_pnl": D(r["realized_pnl"]) + pnl,
           "fees": D(r["fees"]) + fees, "allocated": D(r["allocated"]) + allocated}
    conn.execute(
        "UPDATE budget_pools SET cash = ?, reserved = ?, invested = ?, realized_pnl = ?, fees = ?, allocated = ?, "
        "updated_at = ? WHERE automation_id = ?",
        (*(str(new[k]) for k in ("cash", "reserved", "invested", "realized_pnl", "fees", "allocated")),
         now_iso(), automation_id),
    )
    conn.execute(
        "INSERT INTO budget_entries(id, automation_id, type, amount, cash_after, reserved_after, run_id, order_id, "
        "fill_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("bge"), automation_id, type_, str(amount), str(new["cash"]), str(new["reserved"]), run_id, order_id,
         fill_id, note, now_iso()),
    )
    return get_pool(conn, automation_id)


def allocate(conn, automation_id: str, portfolio: dict, amount: Decimal, note: str | None = None) -> dict:
    """Move money from the portfolio's unallocated cash into the automation's budget, under the write lock.

    Args:
        conn: app DB connection (this function runs its own locked transaction).
        automation_id: the automation's id.
        portfolio: the automation's portfolio.
        amount: > 0.
        note: ledger note.

    Raises:
        BudgetError: amount <= 0, or more than the unallocated cash.

    Used by: routes.allocate.
    """
    if amount <= 0:
        raise BudgetError("amount must be positive")
    with immediate(conn):
        ensure_pool(conn, automation_id, portfolio)
        free = unallocated_cash(conn, portfolio)
        if amount > free:
            raise BudgetError(f"only {free:.2f} {portfolio['currency']} of the portfolio's cash is unallocated")
        return _apply(conn, automation_id, "ALLOCATE", amount, cash=amount, allocated=amount, note=note)


def withdraw(conn, automation_id: str, amount: Decimal, note: str | None = None) -> dict:
    """Give available budget (not reserved, not invested) back to the portfolio, under the write lock.

    Args:
        conn: app DB connection (this function runs its own locked transaction).
        automation_id: the automation's id.
        amount: > 0.
        note: ledger note.

    Raises:
        BudgetError: amount <= 0, or more than available.

    Used by: routes.withdraw.
    """
    if amount <= 0:
        raise BudgetError("amount must be positive")
    with immediate(conn):
        free = available(conn, automation_id)
        if amount > free:
            raise BudgetError(f"only {free:.2f} is available (the rest is reserved or invested)")
        return _apply(conn, automation_id, "WITHDRAW", -amount, cash=-amount, allocated=-amount, note=note)


def reserve(conn, automation_id: str, amount: Decimal, order_id: str, run_id: str | None) -> None:
    """Set money aside for a buy order (RESERVE entry; cash unchanged).

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        amount: estimated cost (with the price buffer for market/stop orders).
        order_id: the order.
        run_id: the run that placed it, if any.

    Used by: orders.service.place_order.
    """
    _apply(conn, automation_id, "RESERVE", amount, reserved=amount, order_id=order_id, run_id=run_id)


def release(conn, automation_id: str, amount: Decimal, order_id: str, note: str) -> None:
    """Give back the unused part of a reservation (RELEASE entry) when an order finishes. No-op for 0.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        amount: unused reservation.
        order_id: the order.
        note: e.g. "order filled", "order cancelled".

    Used by: orders.service.finalize.
    """
    if amount > 0:
        _apply(conn, automation_id, "RELEASE", -amount, reserved=-amount, order_id=order_id, note=note)


def buy_fill(conn, automation_id: str, cost: Decimal, reserve_used: Decimal, order_id: str, fill_id: str) -> None:
    """A buy filled: cash down by the cost, reservation used, invested up (BUY_FILL entry).

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        cost: quantity x price.
        reserve_used: part of the order's reservation this fill used.
        order_id: the order.
        fill_id: the fill.

    Used by: orders.service.apply_fill.
    """
    _apply(conn, automation_id, "BUY_FILL", -cost, cash=-cost, reserved=-reserve_used, invested=cost,
           order_id=order_id, fill_id=fill_id)


def sell_fill(conn, automation_id: str, proceeds: Decimal, cost_basis: Decimal, pnl: Decimal, order_id: str | None,
              fill_id: str) -> None:
    """A sell filled: cash up by the proceeds, invested down by the lots' cost, realized P&L recorded (SELL_FILL).

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        proceeds: quantity sold from the automation's lots x price.
        cost_basis: what those shares cost.
        pnl: proceeds - cost_basis.
        order_id: the order (None for outside sells).
        fill_id: the fill.

    Used by: orders.service.apply_fill.
    """
    _apply(conn, automation_id, "SELL_FILL", proceeds, cash=proceeds, invested=-cost_basis, pnl=pnl,
           order_id=order_id, fill_id=fill_id)


def fee(conn, automation_id: str, amount: Decimal, order_id: str | None, fill_id: str) -> None:
    """A fill's fee (FEE entry). No-op for 0.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        amount: the fee.
        order_id: the order.
        fill_id: the fill.

    Used by: orders.service.apply_fill.
    """
    if amount > 0:
        _apply(conn, automation_id, "FEE", -amount, cash=-amount, fees=amount, order_id=order_id, fill_id=fill_id)


def holdings_blocking_removal(conn, automation_id: str) -> list[str]:
    """What still ties an automation to its portfolio besides spare budget cash: shares and open orders.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.

    Returns:
        Readable reasons; empty when there are none.

    Used by: routes.delete_automation, routes.update_automation (moving portfolios), repair_orphan_pools.
    """
    reasons = []
    shares = conn.execute("SELECT symbol, remaining_quantity FROM position_lots WHERE automation_id = ? "
                          "AND closed_at IS NULL", (automation_id,)).fetchall()
    if shares:
        reasons.append("it holds shares (" + ", ".join(f"{D(r['remaining_quantity'])} {r['symbol']}" for r in shares)
                       + "): sell them first")
    open_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE automation_id = ? AND status IN "
                               "('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')", (automation_id,)).fetchone()[0]
    if open_orders:
        reasons.append(f"it has {open_orders} open order(s): cancel them first")
    return reasons


def close_pool(conn, automation_id: str, note: str) -> Decimal:
    """Return the pool's remaining cash to its portfolio and remove the pool (the ledger is kept).
    Only for a pool with nothing reserved or invested.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        note: ledger note for the WITHDRAW entry.

    Returns:
        The amount returned.

    Raises:
        BudgetError: something is still reserved or invested.

    Used by: routes.delete_automation, routes.update_automation, repair_orphan_pools.
    """
    r = _row(conn, automation_id)
    if r is None:
        return D0
    if D(r["reserved"]) or D(r["invested"]):
        raise BudgetError("the budget still has reserved or invested money")
    cash = D(r["cash"])
    if cash:
        _apply(conn, automation_id, "WITHDRAW", -cash, cash=-cash, allocated=-cash, note=note)
    conn.execute("DELETE FROM budget_pools WHERE automation_id = ?", (automation_id,))
    return cash


def repair_orphan_pools(conn) -> list[dict]:
    """Budgets left behind by automations deleted before deletion was guarded: give their cash back to the
    portfolio. Pools that still hold shares or reservations are left alone (and reported).

    Args:
        conn: app DB connection.

    Returns:
        [{automationId, repaired, returned?}] for each orphan found.

    Used by: api.create_app (startup).
    """
    out = []
    rows = conn.execute("SELECT * FROM budget_pools p WHERE NOT EXISTS "
                        "(SELECT 1 FROM automations a WHERE a.id = p.automation_id)").fetchall()
    for r in rows:
        aid = r["automation_id"]
        if D(r["reserved"]) or D(r["invested"]) or holdings_blocking_removal(conn, aid):
            out.append({"automationId": aid, "repaired": False})
            continue
        with immediate(conn):
            amount = close_pool(conn, aid, "returned to the portfolio: the automation was deleted")
        out.append({"automationId": aid, "repaired": True, "returned": float(amount)})
    return out


def entries(conn, automation_id: str, limit: int = 200) -> list[dict]:
    """An automation's budget ledger, newest first.

    Args:
        conn: app DB connection (caller manages the transaction unless noted).
        automation_id: the automation's id.
        limit: maximum entries.

    Used by: routes.get_budget (and the other budget routes' responses).
    """
    rows = conn.execute("SELECT * FROM budget_entries WHERE automation_id = ? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT ?", (automation_id, limit))
    return [{"id": r["id"], "type": r["type"], "amount": num(r["amount"]), "cashAfter": num(r["cash_after"]),
             "reservedAfter": num(r["reserved_after"]), "runId": r["run_id"], "orderId": r["order_id"],
             "fillId": r["fill_id"], "note": r["note"], "createdAt": r["created_at"]} for r in rows]
