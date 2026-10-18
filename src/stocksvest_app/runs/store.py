"""Run history: one row per execution of any app action (FILTER, FILTER_GROUP; later AUTOMATION, ORDER, ...).

`definition` is a copy of the configuration at run time and `result` includes the prices at run time,
so later analysis can compare what was selected with what happened next.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    subject_id  TEXT,
    ran_at      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status      TEXT NOT NULL,             -- SUCCEEDED | PARTIAL | FAILED | SKIPPED
    definition  TEXT NOT NULL,
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS runs_subject ON runs(kind, subject_id, ran_at);
CREATE INDEX IF NOT EXISTS runs_ran_at ON runs(ran_at);
"""


def run_json(r: sqlite3.Row, include_result: bool = True) -> dict:
    """A runs row -> API shape.

    Args:
        r: row from the runs table.
        include_result: add `definition` and `result` (large); listings leave them out.

    Returns:
        {id, kind, subjectId, ranAt, durationMs, status, error[, definition, result]}.

    Used by: get_run, list_runs.
    """
    out = {
        "id": r["id"],
        "kind": r["kind"],
        "subjectId": r["subject_id"],
        "ranAt": r["ran_at"],
        "durationMs": r["duration_ms"],
        "status": r["status"],
        "error": r["error"],
    }
    if include_result:
        out["definition"] = json.loads(r["definition"])
        out["result"] = json.loads(r["result"]) if r["result"] else None
    return out


def add_run(conn, kind: str, subject_id: str | None, definition: dict, duration_ms: int,
            result: dict | None = None, error: str | None = None, retention_days: int = 183,
            status: str | None = None, run_id: str | None = None) -> dict:
    """Store a run and drop runs older than the retention window.
    `status` defaults to FAILED when there is an error, else SUCCEEDED (automations also use PARTIAL and SKIPPED).

    Args:
        conn: app DB connection.
        kind: what ran: "FILTER", "FILTER_GROUP" or "AUTOMATION".
        subject_id: the filter, group or automation id.
        definition: copy of its configuration at run time (kept even if it is edited or deleted later).
        duration_ms: how long the run took.
        result: what it produced (steps, symbols with prices, actions, ...); None for failures.
        error: failure message; sets status FAILED unless `status` is given.
        retention_days: runs older than this are deleted now (Settings.run_retention_days).
        status: "SUCCEEDED", "PARTIAL", "FAILED" or "SKIPPED"; default from `error`.
        run_id: use this id (automation runs derive client order ids from it); default a new one.

    Returns:
        The stored run, with definition and result.

    Used by: runs.routes.record (filters and groups), automations.runner (automation runs),
    automations.scheduler (SKIPPED/MISSED records).
    """
    run_id = run_id or new_id("run")
    with conn:
        conn.execute(
            "INSERT INTO runs(id, kind, subject_id, ran_at, duration_ms, status, definition, result, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, subject_id, now_iso(), duration_ms, status or ("FAILED" if error else "SUCCEEDED"),
             json.dumps(definition), json.dumps(result) if result is not None else None, error),
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("DELETE FROM runs WHERE ran_at < ?", (cutoff,))
    return get_run(conn, run_id)


def list_runs(conn, kind: str | None = None, subject_id: str | None = None, since: str | None = None,
              limit: int = 50, include_result: bool = False) -> list[dict]:
    """Runs, newest first.

    Args:
        conn: app DB connection.
        kind: "FILTER", "FILTER_GROUP" or "AUTOMATION"; None = all kinds.
        subject_id: one filter/group/automation; None = all.
        since: ISO time; only runs at or after it.
        limit: maximum number of runs.
        include_result: add definition and result to each run.

    Used by: runs.routes.list_runs, filters.routes (filter and group run history), automations.routes (run history,
    last run).
    """
    where, args = [], []
    for col, val in (("kind", kind), ("subject_id", subject_id)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if since:
        where.append("ran_at >= ?")
        args.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"SELECT * FROM runs {clause} ORDER BY ran_at DESC, rowid DESC LIMIT ?", args + [limit])
    return [run_json(r, include_result) for r in rows]


def get_run(conn, run_id: str) -> dict | None:
    """One run with its definition and result, or None.

    Args:
        conn: app DB connection.
        run_id: the run's id.

    Used by: add_run, runs.routes.get_run.
    """
    r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return run_json(r) if r else None
