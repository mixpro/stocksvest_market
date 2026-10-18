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

-- An automation's money and shares at the end of each trading day.
CREATE TABLE IF NOT EXISTS automation_snapshots (
    automation_id   TEXT NOT NULL,
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
    PRIMARY KEY (automation_id, date)
);
"""

PORTFOLIO_FIELDS = ("allocated", "cash", "unallocated", "invested", "positions_value", "equity", "realized_pnl",
                    "unrealized_pnl")
AUTOMATION_FIELDS = ("allocated", "cash", "reserved", "invested", "positions_value", "equity", "realized_pnl",
                     "unrealized_pnl", "fees")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.capitalize() for w in rest).replace("Pnl", "PnL")


def insert_portfolio(conn, portfolio_id: str, at: str, date: str, kind: str, values: dict) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO portfolio_snapshots(portfolio_id, at, date, kind, {', '.join(PORTFOLIO_FIELDS)}) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' * len(PORTFOLIO_FIELDS))})",
        (portfolio_id, at, date, kind, *(str(values[f]) for f in PORTFOLIO_FIELDS)),
    )


def insert_automation(conn, automation_id: str, date: str, at: str, values: dict) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO automation_snapshots(automation_id, date, at, portfolio_id, "
        f"{', '.join(AUTOMATION_FIELDS)}, return_percent, open_positions, orders_placed, fills) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' * len(AUTOMATION_FIELDS))}, ?, ?, ?, ?)",
        (automation_id, date, at, values["portfolio_id"], *(str(values[f]) for f in AUTOMATION_FIELDS),
         str(values["return_percent"]) if values["return_percent"] is not None else None,
         values["open_positions"], values["orders_placed"], values["fills"]),
    )


def _row_json(r: sqlite3.Row, fields) -> dict:
    out = {"at": r["at"], "date": r["date"]}
    for f in fields:
        out[_camel(f)] = float(r[f])
    return out


def portfolio_points(conn, portfolio_id: str, start: str | None, end: str | None, kind: str | None) -> list[dict]:
    sql, args = "SELECT * FROM portfolio_snapshots WHERE portfolio_id = ?", [portfolio_id]
    for cond, val in (("at >= ?", start), ("at <= ?", end), ("kind = ?", kind)):
        if val:
            sql += f" AND {cond}"
            args.append(val)
    return [{**_row_json(r, PORTFOLIO_FIELDS), "kind": r["kind"]} for r in conn.execute(sql + " ORDER BY at", args)]


def automation_points(conn, automation_id: str, start: str | None, end: str | None) -> list[dict]:
    sql, args = "SELECT * FROM automation_snapshots WHERE automation_id = ?", [automation_id]
    for cond, val in (("date >= ?", start), ("date <= ?", end)):
        if val:
            sql += f" AND {cond}"
            args.append(val[:10])
    out = []
    for r in conn.execute(sql + " ORDER BY date", args):
        out.append({**_row_json(r, AUTOMATION_FIELDS),
                    "returnPercent": float(r["return_percent"]) if r["return_percent"] is not None else None,
                    "openPositions": r["open_positions"], "ordersPlaced": r["orders_placed"], "fills": r["fills"]})
    return out


def has_portfolio_snapshot(conn, portfolio_id: str, kind: str, since: str | None = None, date: str | None = None) -> bool:
    sql, args = "SELECT 1 FROM portfolio_snapshots WHERE portfolio_id = ? AND kind = ?", [portfolio_id, kind]
    if since:
        sql += " AND at >= ?"
        args.append(since)
    if date:
        sql += " AND date = ?"
        args.append(date)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None
