"""Budget pools: the money an automation may trade with, carved out of its portfolio's cash.

Every change is a ledger entry with the balances after it, so the history reads like a bank statement.
  cash      uninvested money of the pool (includes the reserved part)
  reserved  set aside for open buy orders
  available cash - reserved (what new buys may use)
  invested  cost basis of the shares the automation holds
"""

import sqlite3
from decimal import Decimal

from ..db import new_id, now_iso
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
    pass


def _row(conn, automation_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM budget_pools WHERE automation_id = ?", (automation_id,)).fetchone()


def pool_json(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    cash, reserved = D(r["cash"]), D(r["reserved"])
    return {"automationId": r["automation_id"], "portfolioId": r["portfolio_id"], "currency": r["currency"],
            "allocated": num(r["allocated"]), "cash": num(cash), "reserved": num(reserved),
            "available": num(cash - reserved), "invested": num(r["invested"]), "realizedPnL": num(r["realized_pnl"]),
            "fees": num(r["fees"]), "updatedAt": r["updated_at"]}


def get_pool(conn, automation_id: str) -> dict | None:
    return pool_json(_row(conn, automation_id))


def available(conn, automation_id: str) -> Decimal:
    r = _row(conn, automation_id)
    return D(r["cash"]) - D(r["reserved"]) if r else D0


def ensure_pool(conn, automation_id: str, portfolio: dict) -> None:
    r = _row(conn, automation_id)
    if r and r["portfolio_id"] != portfolio["id"]:
        if D(r["cash"]) or D(r["invested"]) or D(r["reserved"]):
            raise BudgetError("the budget still holds money or shares in another portfolio; withdraw it first")
        conn.execute("DELETE FROM budget_pools WHERE automation_id = ?", (automation_id,))
        r = None
    if r is None:
        conn.execute("INSERT INTO budget_pools(automation_id, portfolio_id, currency, updated_at) VALUES (?, ?, ?, ?)",
                     (automation_id, portfolio["id"], portfolio["currency"], now_iso()))


def unallocated_cash(conn, portfolio: dict, open_manual_buys: Decimal = D0) -> Decimal:
    """Portfolio cash not given to any automation pool and not reserved by manual buy orders."""
    pools = conn.execute("SELECT cash FROM budget_pools WHERE portfolio_id = ?", (portfolio["id"],)).fetchall()
    return D(portfolio["cash"]) - sum((D(p["cash"]) for p in pools), D0) - open_manual_buys


def _apply(conn, automation_id: str, type_: str, amount: Decimal, *, cash: Decimal = D0, reserved: Decimal = D0,
           invested: Decimal = D0, pnl: Decimal = D0, fees: Decimal = D0, allocated: Decimal = D0,
           run_id=None, order_id=None, fill_id=None, note=None) -> dict:
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


def allocate(conn, automation_id: str, portfolio: dict, amount: Decimal, note: str | None = None,
             open_manual_buys: Decimal = D0) -> dict:
    if amount <= 0:
        raise BudgetError("amount must be positive")
    with conn:
        ensure_pool(conn, automation_id, portfolio)
        free = unallocated_cash(conn, portfolio, open_manual_buys)
        if amount > free:
            raise BudgetError(f"only {free:.2f} {portfolio['currency']} of the portfolio's cash is unallocated")
        return _apply(conn, automation_id, "ALLOCATE", amount, cash=amount, allocated=amount, note=note)


def withdraw(conn, automation_id: str, amount: Decimal, note: str | None = None) -> dict:
    if amount <= 0:
        raise BudgetError("amount must be positive")
    with conn:
        free = available(conn, automation_id)
        if amount > free:
            raise BudgetError(f"only {free:.2f} is available (the rest is reserved or invested)")
        return _apply(conn, automation_id, "WITHDRAW", -amount, cash=-amount, allocated=-amount, note=note)


def reserve(conn, automation_id: str, amount: Decimal, order_id: str, run_id: str | None) -> None:
    _apply(conn, automation_id, "RESERVE", amount, reserved=amount, order_id=order_id, run_id=run_id)


def release(conn, automation_id: str, amount: Decimal, order_id: str, note: str) -> None:
    if amount > 0:
        _apply(conn, automation_id, "RELEASE", -amount, reserved=-amount, order_id=order_id, note=note)


def buy_fill(conn, automation_id: str, cost: Decimal, reserve_used: Decimal, order_id: str, fill_id: str) -> None:
    _apply(conn, automation_id, "BUY_FILL", -cost, cash=-cost, reserved=-reserve_used, invested=cost,
           order_id=order_id, fill_id=fill_id)


def sell_fill(conn, automation_id: str, proceeds: Decimal, cost_basis: Decimal, pnl: Decimal, order_id: str | None,
              fill_id: str) -> None:
    _apply(conn, automation_id, "SELL_FILL", proceeds, cash=proceeds, invested=-cost_basis, pnl=pnl,
           order_id=order_id, fill_id=fill_id)


def fee(conn, automation_id: str, amount: Decimal, order_id: str | None, fill_id: str) -> None:
    if amount > 0:
        _apply(conn, automation_id, "FEE", -amount, cash=-amount, fees=amount, order_id=order_id, fill_id=fill_id)


def entries(conn, automation_id: str, limit: int = 200) -> list[dict]:
    rows = conn.execute("SELECT * FROM budget_entries WHERE automation_id = ? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT ?", (automation_id, limit))
    return [{"id": r["id"], "type": r["type"], "amount": num(r["amount"]), "cashAfter": num(r["cash_after"]),
             "reservedAfter": num(r["reserved_after"]), "runId": r["run_id"], "orderId": r["order_id"],
             "fillId": r["fill_id"], "note": r["note"], "createdAt": r["created_at"]} for r in rows]
