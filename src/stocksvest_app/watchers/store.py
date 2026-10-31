"""Events found by the watchers, and each watcher's on/off switch, settings and saved state.

Design: UML/Watchers.mmd, UML/EventLifecycle.mmd. A watcher's config lives in app_settings under "watcher:<name>".
"""

import json

from .. import db
from ..alerts.checks import _get, _put

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,         -- the same finding never fires twice, e.g. drop:INTC:2026-10-30:2
    watcher       TEXT NOT NULL,
    kind          TEXT NOT NULL,                -- HOLDING_DROP | MARKET_DROP | SECTOR_DROP | ...
    symbol        TEXT,
    sector        TEXT,
    severity      INTEGER NOT NULL,             -- 1..5
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    data          TEXT NOT NULL,                -- JSON
    status        TEXT NOT NULL DEFAULT 'NEW',  -- NEW | AWAITING_CONFIRM | ACTING | DONE | FAILED | DECLINED | EXPIRED
    status_reason TEXT,
    outcomes      TEXT NOT NULL DEFAULT '[]',   -- JSON: per matching automation {automationId, name, status, ...}
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_status ON events(status, created_at);
"""


def add(conn, watcher: str, event: dict) -> str | None:
    """Store one event a watcher returned, unless its key is already stored.

    Args:
        conn: app DB connection.
        watcher: the watcher's name.
        event: {key, kind, severity, title, body, symbol?, sector?, data?}.

    Returns:
        The new event id, or None when the key was already there.

    Used by: watchers.thread.tick.
    """
    eid = db.new_id("evt")
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(id, key, watcher, kind, symbol, sector, severity, title, body, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, event["key"], watcher, event["kind"], event.get("symbol"), event.get("sector"), event["severity"],
             event["title"], event["body"], json.dumps(event.get("data") or {}), db.now_iso()))
    return eid if cur.rowcount else None


def _row(r) -> dict:
    return {"id": r["id"], "key": r["key"], "watcher": r["watcher"], "kind": r["kind"], "symbol": r["symbol"],
            "sector": r["sector"], "severity": r["severity"], "title": r["title"], "body": r["body"],
            "data": json.loads(r["data"]), "status": r["status"], "statusReason": r["status_reason"],
            "outcomes": json.loads(r["outcomes"]), "createdAt": r["created_at"]}


def get_event(conn, event_id: str) -> dict | None:
    """Used by: dispatch.confirm, routes."""
    r = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row(r) if r else None


def list_events(conn, status: str | None = None, limit: int = 100) -> list[dict]:
    """Events, newest first.

    Args:
        conn: app DB connection.
        status: only this status.
        limit: at most this many.

    Used by: routes (GET /v1/events), thread.dispatch (status NEW).
    """
    sql, args = "SELECT * FROM events", []
    if status:
        sql, args = sql + " WHERE status = ?", [status]
    return [_row(r) for r in conn.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit))]


def set_status(conn, event_id: str, status: str, reason: str | None = None, outcomes: list | None = None) -> None:
    """Used by: dispatch."""
    with conn:
        if outcomes is None:
            conn.execute("UPDATE events SET status = ?, status_reason = ? WHERE id = ?", (status, reason, event_id))
        else:
            conn.execute("UPDATE events SET status = ?, status_reason = ?, outcomes = ? WHERE id = ?",
                         (status, reason, json.dumps(outcomes), event_id))


def claim(conn, event_id: str, from_status: str) -> bool:
    """Move an event from `from_status` to ACTING, unless something else already did (a second confirm tap, the
    expiry pass): only one caller gets True.

    Used by: dispatch.confirm, dispatch.expire.
    """
    with conn:
        return conn.execute("UPDATE events SET status = 'ACTING' WHERE id = ? AND status = ?",
                            (event_id, from_status)).rowcount == 1


def config(conn, watcher) -> dict:
    """A watcher's stored config merged over its defaults: {enabled, settings, state, lastRunAt, lastError}.

    Args:
        conn: app DB connection.
        watcher: the watcher module (its NAME, DEFAULTS, ENABLED).

    Used by: thread.tick, routes.
    """
    c = _get(conn, f"watcher:{watcher.NAME}") or {}
    return {"enabled": c.get("enabled", watcher.ENABLED), "settings": {**watcher.DEFAULTS, **c.get("settings", {})},
            "state": c.get("state", {}), "lastRunAt": c.get("lastRunAt"), "lastError": c.get("lastError")}


def save_config(conn, watcher, **changes) -> dict:
    """Change some config fields (settings keep only the watcher's own keys).

    Used by: thread.tick (state, lastRunAt, lastError), routes (enabled, settings).
    """
    c = {**config(conn, watcher), **changes}
    c["settings"] = {k: v for k, v in c["settings"].items() if k in watcher.DEFAULTS}
    _put(conn, f"watcher:{watcher.NAME}", c)
    return c


def held(conn) -> dict[str, list[str]]:
    """symbol -> names of the portfolios holding it (open lots).

    Used by: holdings_drop, news.
    """
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT DISTINCT l.symbol, p.name FROM position_lots l JOIN portfolios p "
                          "ON p.id = l.portfolio_id WHERE l.closed_at IS NULL"):
        out.setdefault(r["symbol"], []).append(r["name"])
    return out
