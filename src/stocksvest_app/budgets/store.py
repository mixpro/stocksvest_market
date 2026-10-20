"""Budgets: named pots of money carved out of a portfolio's cash (UML/InvestmentPortfolio.mmd).

A portfolio can have several budgets. The portfolio decides which automations use each budget, and one
automation can be in several budgets (of one or more portfolios). Every automation in a budget shares its money:
buys take from it, sells give the money back. Shares bought with a budget belong to it (position_lots.budget_id),
and only that budget's automations sell them.

Every change is a ledger entry with the balances after it, so the history reads like a bank statement.
  cash      uninvested money of the budget (includes the reserved part)
  reserved  set aside for open buy orders
  available cash - reserved (what new buys may use)
  invested  cost basis of the shares the budget holds
"""

import sqlite3
from decimal import Decimal

from ..db import immediate, new_id, now_iso
from ..portfolios.store import D, D0, num

SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    id            TEXT PRIMARY KEY,
    portfolio_id  TEXT NOT NULL REFERENCES portfolios(id),
    name          TEXT NOT NULL,
    currency      TEXT NOT NULL,
    allocated     TEXT NOT NULL DEFAULT '0',   -- money put in minus money taken out
    cash          TEXT NOT NULL DEFAULT '0',
    reserved      TEXT NOT NULL DEFAULT '0',
    invested      TEXT NOT NULL DEFAULT '0',
    realized_pnl  TEXT NOT NULL DEFAULT '0',
    fees          TEXT NOT NULL DEFAULT '0',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (portfolio_id, name)
);

-- Which automations trade with which budget (many-to-many).
CREATE TABLE IF NOT EXISTS budget_automations (
    budget_id     TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (budget_id, automation_id)
);
CREATE INDEX IF NOT EXISTS budget_automations_by_automation ON budget_automations(automation_id);

-- Kept permanently, also after the budget is deleted.
CREATE TABLE IF NOT EXISTS budget_entries (
    id             TEXT PRIMARY KEY,
    budget_id      TEXT NOT NULL,
    automation_id  TEXT,                       -- the automation whose order caused it, if any
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
"""  # budget_entries_by_budget is created by db.init (INDEXES), after older ledgers are upgraded

OPEN_STATUSES = "('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')"  # SQL list of open order statuses. Used by: this module


class BudgetError(ValueError):
    """A budget change that isn't possible (not enough money, name taken, still holds shares, ...).

    Used by: this module (raised); budgets.routes (-> 409/422), orders and runner (missing budget).
    """
    pass


def _row(conn, budget_id: str) -> sqlite3.Row | None:
    """The budgets row, or None.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
    """
    return conn.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()


def automations_of(conn, budget_id: str) -> list[dict]:
    """The automations attached to a budget, by name: [{id, name, status}].

    Args:
        conn: app DB connection.
        budget_id: the budget's id.

    Used by: budget_json, budgets.routes.
    """
    rows = conn.execute("SELECT a.id, a.name, a.status FROM budget_automations m JOIN automations a "
                        "ON a.id = m.automation_id WHERE m.budget_id = ? ORDER BY a.name", (budget_id,))
    return [{"id": r["id"], "name": r["name"], "status": r["status"]} for r in rows]


def budget_json(conn, r: sqlite3.Row | None) -> dict | None:
    """A budgets row -> API shape {id, portfolioId, name, currency, allocated, cash, reserved, available, invested,
    realizedPnL, fees, automations, createdAt, updatedAt}; None stays None.

    Args:
        conn: app DB connection (for the attached automations).
        r: the row or None.

    Used by: get_budget, list_budgets.
    """
    if r is None:
        return None
    cash, reserved = D(r["cash"]), D(r["reserved"])
    return {"id": r["id"], "portfolioId": r["portfolio_id"], "name": r["name"], "currency": r["currency"],
            "allocated": num(r["allocated"]), "cash": num(cash), "reserved": num(reserved),
            "available": num(cash - reserved), "invested": num(r["invested"]), "realizedPnL": num(r["realized_pnl"]),
            "fees": num(r["fees"]), "automations": automations_of(conn, r["id"]),
            "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


def get_budget(conn, budget_id: str) -> dict | None:
    """One budget, or None.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.

    Used by: budgets.routes, runner.Trading, portfolios.routes.place_manual_order.
    """
    return budget_json(conn, _row(conn, budget_id))


def list_budgets(conn, portfolio_id: str | None = None) -> list[dict]:
    """Budgets by name, of one portfolio or of all.

    Args:
        conn: app DB connection.
        portfolio_id: only this portfolio's; None = all.

    Used by: budgets.routes, portfolios.routes (summary, delete guard).
    """
    if portfolio_id:
        rows = conn.execute("SELECT * FROM budgets WHERE portfolio_id = ? ORDER BY name", (portfolio_id,))
    else:
        rows = conn.execute("SELECT * FROM budgets ORDER BY portfolio_id, name")
    return [budget_json(conn, r) for r in rows.fetchall()]


def budgets_of(conn, automation_id: str) -> list[dict]:
    """The budgets an automation trades with, in a fixed order (portfolio, then budget id): what a run walks
    through, and the order its portfolio locks are taken in.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.

    Used by: runner.run_automation, automations.routes (the automation's "budgets"), record.
    """
    rows = conn.execute("SELECT b.* FROM budget_automations m JOIN budgets b ON b.id = m.budget_id "
                        "WHERE m.automation_id = ? ORDER BY b.portfolio_id, b.id", (automation_id,))
    return [budget_json(conn, r) for r in rows.fetchall()]


def available(conn, budget_id: str) -> Decimal:
    """cash - reserved: what new buys can use (0 for an unknown budget).

    Args:
        conn: app DB connection.
        budget_id: the budget's id.

    Used by: orders.service._check_funds, withdraw.
    """
    r = _row(conn, budget_id)
    return D(r["cash"]) - D(r["reserved"]) if r else D0


def unallocated_cash(conn, portfolio: dict, open_manual_buys: Decimal | None = None) -> Decimal:
    """Portfolio cash not in any budget and not reserved by manual buy orders.
    Reads the current cash from the DB (not the possibly stale `portfolio` dict).

    Args:
        conn: app DB connection.
        portfolio: the portfolio (its id is used).
        open_manual_buys: money held by open manual buys; None = compute it now.

    Used by: create_budget, allocate, orders.service._check_funds (manual buys), portfolios routes (withdraw,
    summary), performance.snapshots.
    """
    if open_manual_buys is None:
        from ..orders.service import open_manual_buys as manual_buys
        open_manual_buys = manual_buys(conn, portfolio["id"])
    cash = conn.execute("SELECT cash FROM portfolios WHERE id = ?", (portfolio["id"],)).fetchone()
    budgets = conn.execute("SELECT cash FROM budgets WHERE portfolio_id = ?", (portfolio["id"],)).fetchall()
    return D(cash["cash"] if cash else 0) - sum((D(b["cash"]) for b in budgets), D0) - open_manual_buys


def _apply(conn, budget_id: str, type_: str, amount: Decimal, *, cash: Decimal = D0, reserved: Decimal = D0,
           invested: Decimal = D0, pnl: Decimal = D0, fees: Decimal = D0, allocated: Decimal = D0,
           automation_id=None, run_id=None, order_id=None, fill_id=None, note=None) -> None:
    """Change a budget's balances and append a ledger entry with the balances after it.

    Args:
        conn: app DB connection (caller manages the transaction).
        budget_id: the budget's id.
        type_: ALLOCATE, WITHDRAW, RESERVE, RELEASE, BUY_FILL, SELL_FILL, FEE or ADJUSTMENT.
        amount: the entry's signed amount (change of cash; for RESERVE/RELEASE, change of reserved).
        cash: change of cash.
        reserved: change of reserved.
        invested: change of invested.
        pnl: change of realized P&L.
        fees: change of fees.
        allocated: change of allocated.
        automation_id: the automation whose order caused it.
        run_id: related automation run.
        order_id: related order.
        fill_id: related fill.
        note: free text.

    Raises:
        BudgetError: the budget doesn't exist.
    """
    r = _row(conn, budget_id)
    if r is None:
        raise BudgetError(f"budget '{budget_id}' does not exist")
    new = {"cash": D(r["cash"]) + cash, "reserved": D(r["reserved"]) + reserved,
           "invested": D(r["invested"]) + invested, "realized_pnl": D(r["realized_pnl"]) + pnl,
           "fees": D(r["fees"]) + fees, "allocated": D(r["allocated"]) + allocated}
    conn.execute(
        "UPDATE budgets SET cash = ?, reserved = ?, invested = ?, realized_pnl = ?, fees = ?, allocated = ?, "
        "updated_at = ? WHERE id = ?",
        (*(str(new[k]) for k in ("cash", "reserved", "invested", "realized_pnl", "fees", "allocated")),
         now_iso(), budget_id),
    )
    conn.execute(
        "INSERT INTO budget_entries(id, budget_id, automation_id, type, amount, cash_after, reserved_after, run_id, "
        "order_id, fill_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("bge"), budget_id, automation_id, type_, str(amount), str(new["cash"]), str(new["reserved"]), run_id,
         order_id, fill_id, note, now_iso()),
    )


# ------------------------------------------------------------------ create, rename, delete


def create_budget(conn, portfolio: dict, name: str, amount: Decimal = D0, note: str | None = None) -> dict:
    """Create a budget in a portfolio, optionally funded from its unallocated cash, under the write lock.

    Args:
        conn: app DB connection (this function runs its own locked transaction).
        portfolio: the portfolio.
        name: unique within the portfolio.
        amount: starting money (>= 0).
        note: ledger note for the starting money.

    Returns:
        The new budget.

    Raises:
        BudgetError: the name is taken, or the amount is more than the unallocated cash.

    Used by: budgets.routes.create_budget.
    """
    bid, ts = new_id("bud"), now_iso()
    with immediate(conn):
        if conn.execute("SELECT 1 FROM budgets WHERE portfolio_id = ? AND name = ?", (portfolio["id"], name)).fetchone():
            raise BudgetError(f"this portfolio already has a budget named '{name}'")
        if amount > 0:
            free = unallocated_cash(conn, portfolio)
            if amount > free:
                raise BudgetError(f"only {free:.2f} {portfolio['currency']} of the portfolio's cash is unallocated")
        conn.execute("INSERT INTO budgets(id, portfolio_id, name, currency, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (bid, portfolio["id"], name, portfolio["currency"], ts, ts))
        if amount > 0:
            _apply(conn, bid, "ALLOCATE", amount, cash=amount, allocated=amount, note=note or "starting money")
    return get_budget(conn, bid)


def rename_budget(conn, budget_id: str, name: str) -> dict:
    """Rename a budget.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
        name: the new name, unique within the portfolio.

    Raises:
        BudgetError: the name is taken.

    Used by: budgets.routes.update_budget.
    """
    try:
        with conn:
            conn.execute("UPDATE budgets SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), budget_id))
    except sqlite3.IntegrityError:
        raise BudgetError(f"this portfolio already has a budget named '{name}'")
    return get_budget(conn, budget_id)


def blocking_removal(conn, budget_id: str) -> list[str]:
    """What stops a budget from being deleted: shares it holds and open orders paid from it.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.

    Returns:
        Readable reasons; empty when there are none.

    Used by: delete_budget.
    """
    reasons = []
    shares = conn.execute("SELECT symbol, remaining_quantity FROM position_lots WHERE budget_id = ? "
                          "AND closed_at IS NULL", (budget_id,)).fetchall()
    if shares:
        reasons.append("it holds shares (" + ", ".join(f"{D(r['remaining_quantity'])} {r['symbol']}" for r in shares)
                       + "): sell them first")
    open_orders = conn.execute(f"SELECT COUNT(*) FROM orders WHERE budget_id = ? AND status IN {OPEN_STATUSES}",
                               (budget_id,)).fetchone()[0]
    if open_orders:
        reasons.append(f"it has {open_orders} open order(s): cancel them first")
    return reasons


def delete_budget(conn, budget_id: str) -> Decimal:
    """Delete a budget: its cash goes back to the portfolio's unallocated cash, its automations are detached.
    The ledger is kept. Under the write lock.

    Args:
        conn: app DB connection (this function runs its own locked transaction).
        budget_id: the budget's id.

    Returns:
        The cash returned to the portfolio.

    Raises:
        BudgetError: it still holds shares or has open orders.

    Used by: budgets.routes.delete_budget.
    """
    with immediate(conn):
        reasons = blocking_removal(conn, budget_id)
        if reasons:
            raise BudgetError("can't delete this budget: " + "; ".join(reasons))
        cash = D(_row(conn, budget_id)["cash"])
        if cash:
            _apply(conn, budget_id, "WITHDRAW", -cash, cash=-cash, allocated=-cash,
                   note="returned to the portfolio: the budget was deleted")
        conn.execute("DELETE FROM budget_automations WHERE budget_id = ?", (budget_id,))
        conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    return cash


# ------------------------------------------------------------------ automations in a budget


def add_automation(conn, budget_id: str, automation_id: str) -> bool:
    """Attach an automation to a budget (no-op when already attached).

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
        automation_id: the automation's id (must exist).

    Returns:
        True if it was added now.

    Used by: budgets.routes (create with automationIds, add automation).
    """
    with conn:
        cur = conn.execute("INSERT OR IGNORE INTO budget_automations(budget_id, automation_id, added_at) "
                           "VALUES (?, ?, ?)", (budget_id, automation_id, now_iso()))
    return cur.rowcount == 1


def remove_automation(conn, budget_id: str, automation_id: str) -> bool:
    """Detach an automation from a budget. Its open orders stay with the budget and settle normally.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
        automation_id: the automation's id.

    Returns:
        True if it was attached.

    Used by: budgets.routes.remove_automation.
    """
    with conn:
        cur = conn.execute("DELETE FROM budget_automations WHERE budget_id = ? AND automation_id = ?",
                           (budget_id, automation_id))
    return cur.rowcount == 1


# ------------------------------------------------------------------ money


def allocate(conn, budget_id: str, portfolio: dict, amount: Decimal, note: str | None = None) -> dict:
    """Move money from the portfolio's unallocated cash into the budget, under the write lock.

    Args:
        conn: app DB connection (this function runs its own locked transaction).
        budget_id: the budget's id.
        portfolio: the budget's portfolio.
        amount: > 0.
        note: ledger note.

    Raises:
        BudgetError: amount <= 0, or more than the unallocated cash.

    Used by: budgets.routes.allocate.
    """
    if amount <= 0:
        raise BudgetError("amount must be positive")
    with immediate(conn):
        free = unallocated_cash(conn, portfolio)
        if amount > free:
            raise BudgetError(f"only {free:.2f} {portfolio['currency']} of the portfolio's cash is unallocated")
        _apply(conn, budget_id, "ALLOCATE", amount, cash=amount, allocated=amount, note=note)
    return get_budget(conn, budget_id)


def withdraw(conn, budget_id: str, amount: Decimal, note: str | None = None) -> dict:
    """Give available money (not reserved, not invested) back to the portfolio, under the write lock.

    Args:
        conn: app DB connection (this function runs its own locked transaction).
        budget_id: the budget's id.
        amount: > 0.
        note: ledger note.

    Raises:
        BudgetError: amount <= 0, or more than available.

    Used by: budgets.routes.withdraw.
    """
    if amount <= 0:
        raise BudgetError("amount must be positive")
    with immediate(conn):
        free = available(conn, budget_id)
        if amount > free:
            raise BudgetError(f"only {free:.2f} is available (the rest is reserved or invested)")
        _apply(conn, budget_id, "WITHDRAW", -amount, cash=-amount, allocated=-amount, note=note)
    return get_budget(conn, budget_id)


def reserve(conn, budget_id: str, amount: Decimal, order_id: str, run_id: str | None,
            automation_id: str | None) -> None:
    """Set money aside for a buy order (RESERVE entry; cash unchanged).

    Args:
        conn: app DB connection (caller manages the transaction).
        budget_id: the budget paying for it.
        amount: estimated cost (with the price buffer for market/stop orders).
        order_id: the order.
        run_id: the run that placed it, if any.
        automation_id: the automation that placed it, if any.

    Used by: orders.service.place_order.
    """
    _apply(conn, budget_id, "RESERVE", amount, reserved=amount, order_id=order_id, run_id=run_id,
           automation_id=automation_id)


def release(conn, budget_id: str, amount: Decimal, order_id: str, note: str, automation_id: str | None) -> None:
    """Give back the unused part of a reservation (RELEASE entry) when an order finishes. No-op for 0.

    Args:
        conn: app DB connection (caller manages the transaction).
        budget_id: the budget.
        amount: unused reservation.
        order_id: the order.
        note: e.g. "order filled", "order cancelled".
        automation_id: the automation that placed the order, if any.

    Used by: orders.service.finalize.
    """
    if amount > 0:
        _apply(conn, budget_id, "RELEASE", -amount, reserved=-amount, order_id=order_id, note=note,
               automation_id=automation_id)


def buy_fill(conn, budget_id: str, cost: Decimal, reserve_used: Decimal, order_id: str, fill_id: str,
             automation_id: str | None) -> None:
    """A buy filled: cash down by the cost, reservation used, invested up (BUY_FILL entry).

    Args:
        conn: app DB connection (caller manages the transaction).
        budget_id: the budget that paid.
        cost: quantity x price.
        reserve_used: part of the order's reservation this fill used.
        order_id: the order.
        fill_id: the fill.
        automation_id: the automation that placed the order, if any.

    Used by: orders.service.apply_fill.
    """
    _apply(conn, budget_id, "BUY_FILL", -cost, cash=-cost, reserved=-reserve_used, invested=cost,
           order_id=order_id, fill_id=fill_id, automation_id=automation_id)


def sell_fill(conn, budget_id: str, proceeds: Decimal, cost_basis: Decimal, pnl: Decimal, order_id: str | None,
              fill_id: str, automation_id: str | None) -> None:
    """A sell filled: cash up by the proceeds, invested down by the lots' cost, realized P&L recorded (SELL_FILL).

    Args:
        conn: app DB connection (caller manages the transaction).
        budget_id: the budget whose shares were sold.
        proceeds: quantity sold from the budget's lots x price.
        cost_basis: what those shares cost.
        pnl: proceeds - cost_basis.
        order_id: the order (None for outside sells).
        fill_id: the fill.
        automation_id: the automation that placed the order, if any.

    Used by: orders.service.apply_fill.
    """
    _apply(conn, budget_id, "SELL_FILL", proceeds, cash=proceeds, invested=-cost_basis, pnl=pnl,
           order_id=order_id, fill_id=fill_id, automation_id=automation_id)


def fee(conn, budget_id: str, amount: Decimal, order_id: str | None, fill_id: str | None,
        automation_id: str | None, note: str | None = None) -> None:
    """A fee (FEE entry): of a fill, or the budget's share of an account fee. No-op for 0.

    Args:
        conn: app DB connection (caller manages the transaction).
        budget_id: the budget that pays it.
        amount: the fee (negative = a refund).
        order_id: the order, for fill fees.
        fill_id: the fill, for fill fees.
        automation_id: the automation that placed the order, if any.
        note: e.g. the broker's description of an account fee.

    Used by: orders.service.apply_fill, orders.service.apply_account_fee.
    """
    if amount:
        _apply(conn, budget_id, "FEE", -amount, cash=-amount, fees=amount, order_id=order_id, fill_id=fill_id,
               automation_id=automation_id, note=note)


def entries(conn, budget_id: str, limit: int = 200) -> list[dict]:
    """A budget's ledger, newest first.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
        limit: maximum entries.

    Used by: budgets.routes.ledger.
    """
    rows = conn.execute("SELECT * FROM budget_entries WHERE budget_id = ? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT ?", (budget_id, limit))
    return [{"id": r["id"], "type": r["type"], "amount": num(r["amount"]), "cashAfter": num(r["cash_after"]),
             "reservedAfter": num(r["reserved_after"]), "automationId": r["automation_id"], "runId": r["run_id"],
             "orderId": r["order_id"], "fillId": r["fill_id"], "note": r["note"], "createdAt": r["created_at"]}
            for r in rows]


# ------------------------------------------------------------------ per-automation record


def record(conn, automation_id: str) -> dict:
    """What an automation did with each budget it traded with: orders, money spent and received, realized P&L of
    its sells and fees. Built from its orders and fills, so it covers budgets it has since left or that were deleted.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.

    Returns:
        {"total": {...}, "budgets": [{budgetId, name, portfolioId, ordersPlaced, openOrders, buyFills, spent,
        sellFills, received, realizedPnL, fees}]}.

    Used by: automations.routes.record.
    """
    rows = conn.execute(f"""
        SELECT o.budget_id,
               COUNT(DISTINCT o.id) AS orders_placed,
               COUNT(DISTINCT CASE WHEN o.status IN {OPEN_STATUSES} THEN o.id END) AS open_orders
        FROM orders o WHERE o.automation_id = ? AND o.budget_id IS NOT NULL GROUP BY o.budget_id""",
                        (automation_id,)).fetchall()
    per = {r["budget_id"]: {"ordersPlaced": r["orders_placed"], "openOrders": r["open_orders"], "buyFills": 0,
                            "spent": D0, "sellFills": 0, "received": D0, "realizedPnL": D0, "fees": D0} for r in rows}
    fills = conn.execute("SELECT o.budget_id, f.side, f.quantity, f.price, f.fee, f.realized_pnl FROM fills f "
                         "JOIN orders o ON o.id = f.order_id WHERE o.automation_id = ? AND o.budget_id IS NOT NULL",
                         (automation_id,)).fetchall()
    for f in fills:
        b = per[f["budget_id"]]
        value = D(f["quantity"]) * D(f["price"])
        if f["side"] == "BUY":
            b["buyFills"] += 1
            b["spent"] += value
        else:
            b["sellFills"] += 1
            b["received"] += value
            b["realizedPnL"] += D(f["realized_pnl"])
        b["fees"] += D(f["fee"])
    out = []
    for bid, b in per.items():
        r = conn.execute("SELECT name, portfolio_id FROM budgets WHERE id = ?", (bid,)).fetchone()
        out.append({"budgetId": bid, "name": r["name"] if r else None, "portfolioId": r["portfolio_id"] if r else None,
                    **{k: num(v) if isinstance(v, Decimal) else v for k, v in b.items()}})
    out.sort(key=lambda x: (x["name"] is None, x["name"] or "", x["budgetId"]))
    keys = ("ordersPlaced", "openOrders", "buyFills", "spent", "sellFills", "received", "realizedPnL", "fees")
    total = {k: sum(x[k] for x in out) for k in keys}
    for k in ("spent", "received", "realizedPnL", "fees"):
        total[k] = round(total[k], 2)
    return {"total": total, "budgets": out}
