"""The activity log table and how to write to it.

Two kinds of entries:
  - API requests that change something (written by the middleware, success or failure)
  - things the app does by itself or as a consequence (scheduled runs, orders placed by automations, order
    status changes and fills from the broker, sync failures, startup repairs)
Entries caused by the same API request or automation run share a correlation id.
"""

import json
import logging
import sqlite3
from contextvars import ContextVar

from ..db import connect, now_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    source         TEXT NOT NULL,            -- API | SCHEDULER | SYNC | SYSTEM
    actor          TEXT NOT NULL,            -- key:<fingerprint> for API calls, else the component
    action         TEXT NOT NULL,            -- e.g. FILTER_CREATE, AUTOMATION_RUN, ORDER_PLACED, ORDER_FILLED
    entity_type    TEXT,
    entity_id      TEXT,
    related        TEXT NOT NULL DEFAULT '{}',  -- JSON: other ids involved (portfolioId, automationId, runId, ...)
    outcome        TEXT NOT NULL,            -- OK | FAILED
    error          TEXT,
    details        TEXT NOT NULL DEFAULT '{}',  -- JSON
    method         TEXT,
    path           TEXT,
    status         INTEGER,
    duration_ms    INTEGER,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit_log(at);
CREATE INDEX IF NOT EXISTS audit_by_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS audit_by_correlation ON audit_log(correlation_id);
"""

# Set per API request / automation run, so everything it causes can be linked.
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
current_source: ContextVar[str] = ContextVar("current_source", default="SYSTEM")
current_actor: ContextVar[str] = ContextVar("current_actor", default="system")

MAX_JSON = 8000


def _dump(value) -> str:
    text = json.dumps(value or {}, default=str)
    return text if len(text) <= MAX_JSON else json.dumps({"truncated": text[:MAX_JSON]})


def record(conn: sqlite3.Connection | None, action: str, entity_type: str | None = None, entity_id: str | None = None,
           *, related: dict | None = None, details: dict | None = None, outcome: str = "OK", error: str | None = None,
           source: str | None = None, actor: str | None = None, method: str | None = None, path: str | None = None,
           status: int | None = None, duration_ms: int | None = None, db_path=None,
           correlation: str | None = None) -> None:
    """Write one entry. Never raises: the activity log must not break the operation it describes."""
    own = conn is None
    try:
        if own:
            conn = connect(db_path)
        conn.execute(
            "INSERT INTO audit_log(at, source, actor, action, entity_type, entity_id, related, outcome, error, details, "
            "method, path, status, duration_ms, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), source or current_source.get(), actor or current_actor.get(), action, entity_type, entity_id,
             _dump({k: v for k, v in (related or {}).items() if v}), outcome, error, _dump(details), method, path,
             status, duration_ms, correlation or correlation_id.get()),
        )
        conn.commit()
    except Exception:  # pragma: no cover - logging must never fail the caller
        log.exception("could not write activity log entry %s", action)
    finally:
        if own and conn is not None:
            conn.close()


def entry_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "at": r["at"], "source": r["source"], "actor": r["actor"], "action": r["action"],
            "entityType": r["entity_type"], "entityId": r["entity_id"], "related": json.loads(r["related"]),
            "outcome": r["outcome"], "error": r["error"], "details": json.loads(r["details"]), "method": r["method"],
            "path": r["path"], "status": r["status"], "durationMs": r["duration_ms"],
            "correlationId": r["correlation_id"]}


def query(conn, *, entity_type=None, entity_id=None, related_id=None, action=None, source=None, outcome=None,
          correlation=None, since=None, until=None, before_id=None, limit=100) -> list[dict]:
    where, args = [], []
    for col, val in (("entity_type", entity_type), ("action", action), ("source", source), ("outcome", outcome),
                     ("correlation_id", correlation)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if entity_id:
        where.append("(entity_id = ? OR related LIKE ?)")
        args += [entity_id, f'%"{entity_id}"%']
    if related_id:
        where.append("related LIKE ?")
        args.append(f'%"{related_id}"%')
    if since:
        where.append("at >= ?")
        args.append(since)
    if until:
        where.append("at <= ?")
        args.append(until)
    if before_id:
        where.append("id < ?")
        args.append(before_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return [entry_json(r) for r in conn.execute(f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ?",
                                                args + [limit])]
