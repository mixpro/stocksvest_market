"""Snapshot tables. Kept permanently: they are the series behind charts and gain analysis."""

import sqlite3

SCHEMA = """
-- A portfolio's value at a moment: every 15 minutes during the regular session (INTERVAL), once after the
-- close (CLOSE), or on request (MANUAL).
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    portfolio_id    TEXT NOT NULL,
    at              TEXT NOT NULL,
    date            TEXT NOT NULL,           -- New York trading date
    kind            TEXT NOT NULL,           -- INTERVAL | CLOSE | MANUAL
    allocated       TEXT NOT NULL,
    cash            TEXT NOT NULL,
    unallocated     TEXT NOT NULL,
    invested        TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    equity          TEXT NOT NULL,
    realized_pnl    TEXT NOT NULL,
    unrealized_pnl  TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, at)
);
CREATE INDEX IF NOT EXISTS portfolio_snapshots_by_kind ON portfolio_snapshots(portfolio_id, kind, date);

-- A budget's money and shares at the end of each trading day.
CREATE TABLE IF NOT EXISTS budget_snapshots (
    budget_id       TEXT NOT NULL,
    date            TEXT NOT NULL,
    at              TEXT NOT NULL,
    portfolio_id    TEXT NOT NULL,
    allocated       TEXT NOT NULL,
    cash            TEXT NOT NULL,
    reserved        TEXT NOT NULL,
    invested        TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    equity          TEXT NOT NULL,
    realized_pnl    TEXT NOT NULL,
    unrealized_pnl  TEXT NOT NULL,
    fees            TEXT NOT NULL,
    return_percent  TEXT,
    open_positions  INTEGER NOT NULL,
    orders_placed   INTEGER NOT NULL,
    fills           INTEGER NOT NULL,
    PRIMARY KEY (budget_id, date)
);
"""

# Money columns of each table (stored as decimal text). Used by: insert_*, *_points
PORTFOLIO_FIELDS = ("allocated", "cash", "unallocated", "invested", "positions_value", "equity", "realized_pnl",
                    "unrealized_pnl")
BUDGET_FIELDS = ("allocated", "cash", "reserved", "invested", "positions_value", "equity", "realized_pnl",
                     "unrealized_pnl", "fees")


def _camel(name: str) -> str:
    """snake_case -> camelCase (pnl -> PnL), e.g. realized_pnl -> realizedPnL.

    Args:
        name: the name.

    Used by: _row_json, routes._json.
    """
    head, *rest = name.split("_")
    return head + "".join(w.capitalize() for w in rest).replace("Pnl", "PnL")


def insert_portfolio(conn, portfolio_id: str, at: str, date: str, kind: str, values: dict) -> None:
    """Store a portfolio snapshot (inside the caller's transaction).

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        at: snapshot time, ISO.
        date: New York trading date.
        kind: INTERVAL, CLOSE or MANUAL.
        values: PORTFOLIO_FIELDS values.

    Used by: snapshots.snapshot_portfolio.
    """
    conn.execute(
        f"INSERT OR REPLACE INTO portfolio_snapshots(portfolio_id, at, date, kind, {', '.join(PORTFOLIO_FIELDS)}) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' * len(PORTFOLIO_FIELDS))})",
        (portfolio_id, at, date, kind, *(str(values[f]) for f in PORTFOLIO_FIELDS)),
    )


def insert_budget(conn, budget_id: str, date: str, at: str, values: dict) -> None:
    """Store (or replace) a budget's snapshot of a day (inside the caller's transaction).

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
        date: New York trading date.
        at: snapshot time, ISO.
        values: snapshots.budget_values result.

    Used by: snapshots.snapshot_budgets.
    """
    conn.execute(
        f"INSERT OR REPLACE INTO budget_snapshots(budget_id, date, at, portfolio_id, "
        f"{', '.join(BUDGET_FIELDS)}, return_percent, open_positions, orders_placed, fills) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' * len(BUDGET_FIELDS))}, ?, ?, ?, ?)",
        (budget_id, date, at, values["portfolio_id"], *(str(values[f]) for f in BUDGET_FIELDS),
         str(values["return_percent"]) if values["return_percent"] is not None else None,
         values["open_positions"], values["orders_placed"], values["fills"]),
    )


def _row_json(r: sqlite3.Row, fields) -> dict:
    """A snapshot row in API form.

    Args:
        r: the row.
        fields: money columns to include.

    Used by: portfolio_points, budget_points.
    """
    out = {"at": r["at"], "date": r["date"]}
    for f in fields:
        out[_camel(f)] = float(r[f])
    return out


def portfolio_points(conn, portfolio_id: str, start: str | None, end: str | None, kind: str | None) -> list[dict]:
    """A portfolio's snapshots, oldest first.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        start: from this time.
        end: up to this time.
        kind: only this kind.

    Used by: routes.portfolio_history.
    """
    sql, args = "SELECT * FROM portfolio_snapshots WHERE portfolio_id = ?", [portfolio_id]
    for cond, val in (("at >= ?", start), ("at <= ?", end), ("kind = ?", kind)):
        if val:
            sql += f" AND {cond}"
            args.append(val)
    return [{**_row_json(r, PORTFOLIO_FIELDS), "kind": r["kind"]} for r in conn.execute(sql + " ORDER BY at", args)]


def budget_points(conn, budget_id: str, start: str | None, end: str | None) -> list[dict]:
    """A budget's daily snapshots, oldest first.

    Args:
        conn: app DB connection.
        budget_id: the budget's id.
        start: from this date (first 10 chars used).
        end: up to this date.

    Used by: routes.budget_performance.
    """
    sql, args = "SELECT * FROM budget_snapshots WHERE budget_id = ?", [budget_id]
    for cond, val in (("date >= ?", start), ("date <= ?", end)):
        if val:
            sql += f" AND {cond}"
            args.append(val[:10])
    out = []
    for r in conn.execute(sql + " ORDER BY date", args):
        out.append({**_row_json(r, BUDGET_FIELDS),
                    "returnPercent": float(r["return_percent"]) if r["return_percent"] is not None else None,
                    "openPositions": r["open_positions"], "ordersPlaced": r["orders_placed"], "fills": r["fills"]})
    return out


def has_portfolio_snapshot(conn, portfolio_id: str, kind: str, since: str | None = None, date: str | None = None) -> bool:
    """Whether a portfolio already has a snapshot of a kind in a slot or on a day.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.
        kind: the kind.
        since: at or after this time.
        date: on this trading date.

    Used by: snapshots.scheduled.
    """
    sql, args = "SELECT 1 FROM portfolio_snapshots WHERE portfolio_id = ? AND kind = ?", [portfolio_id, kind]
    if since:
        sql += " AND at >= ?"
        args.append(since)
    if date:
        sql += " AND date = ?"
        args.append(date)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None
