"""Automations tables and queries: definitions, schedule state, fire records, audit events,
notifications and the global switch. Run results live in the shared runs table (kind AUTOMATION)."""

import json
import sqlite3

from ..db import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS automations (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,              -- ACTIVE | PAUSED
    config_version INTEGER NOT NULL,
    definition     TEXT NOT NULL,              -- JSON: runTargets, schedules, actions
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_state (
    schedule_id    TEXT PRIMARY KEY,
    automation_id  TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    last_fired_for TEXT,                       -- last scheduled time handled (fired or skipped)
    next_fire_at   TEXT
);

-- A scheduled time fires once, even after a crash or restart.
CREATE TABLE IF NOT EXISTS schedule_fires (
    id            TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    schedule_id   TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    fired_at      TEXT NOT NULL,
    run_id        TEXT,
    UNIQUE (automation_id, schedule_id, scheduled_for)
);

CREATE TABLE IF NOT EXISTS automation_events (
    id             TEXT PRIMARY KEY,
    automation_id  TEXT NOT NULL,
    type           TEXT NOT NULL,              -- CREATED | UPDATED | PAUSED | RESUMED | DELETED
    config_version INTEGER NOT NULL,
    changes        TEXT NOT NULL,              -- JSON
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS automation_events_by_automation ON automation_events(automation_id, created_at);

CREATE TABLE IF NOT EXISTS notifications (
    id            TEXT PRIMARY KEY,
    automation_id TEXT,
    run_id        TEXT,
    title         TEXT NOT NULL,
    message       TEXT NOT NULL,
    symbols       TEXT NOT NULL,               -- JSON list
    created_at    TEXT NOT NULL,
    read_at       TEXT
);
CREATE INDEX IF NOT EXISTS notifications_by_time ON notifications(created_at);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                        -- JSON
);
"""


# ------------------------------------------------------------------ automations


def automation_json(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"],
        "status": r["status"],
        "configVersion": r["config_version"],
        "portfolioId": None,
        "orderGuards": None,
        **json.loads(r["definition"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def get_automation(conn, automation_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
    return automation_json(r) if r else None


def list_automations(conn, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute("SELECT * FROM automations WHERE status = ? ORDER BY name", (status,))
    else:
        rows = conn.execute("SELECT * FROM automations ORDER BY name")
    return [automation_json(r) for r in rows]


def _definition(data: dict) -> str:
    return json.dumps({k: data.get(k) for k in ("portfolioId", "orderGuards", "runTargets", "schedules", "actions")})


def create_automation(conn, data: dict) -> dict:
    aid, ts = new_id("aut"), now_iso()
    with conn:
        conn.execute(
            "INSERT INTO automations(id, name, description, status, config_version, definition, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ACTIVE', 1, ?, ?, ?)",
            (aid, data["name"], data["description"], _definition(data), ts, ts),
        )
        add_event(conn, aid, "CREATED", 1, {"definition": data})
    return get_automation(conn, aid)


def update_automation(conn, automation_id: str, data: dict) -> dict:
    before = get_automation(conn, automation_id)
    version = before["configVersion"] + 1
    with conn:
        conn.execute(
            "UPDATE automations SET name = ?, description = ?, definition = ?, config_version = ?, updated_at = ? "
            "WHERE id = ?",
            (data["name"], data["description"], _definition(data), version, now_iso(), automation_id),
        )
        # Schedules may have changed: recompute their next fire times from now on.
        conn.execute("DELETE FROM schedule_state WHERE automation_id = ?", (automation_id,))
        changed = {k: {"from": before.get(k), "to": data[k]} for k in data if before.get(k) != data[k]}
        add_event(conn, automation_id, "UPDATED", version, changed)
    return get_automation(conn, automation_id)


def set_status(conn, automation_id: str, status: str) -> dict:
    a = get_automation(conn, automation_id)
    with conn:
        conn.execute("UPDATE automations SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), automation_id))
        # Resuming starts from now: times missed while paused are not replayed.
        conn.execute("DELETE FROM schedule_state WHERE automation_id = ?", (automation_id,))
        add_event(conn, automation_id, "PAUSED" if status == "PAUSED" else "RESUMED", a["configVersion"], {})
    return get_automation(conn, automation_id)


def delete_automation(conn, automation_id: str) -> None:
    a = get_automation(conn, automation_id)
    with conn:
        conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
        add_event(conn, automation_id, "DELETED", a["configVersion"], {"name": a["name"]})


def automations_using(conn, kind: str, target_id: str) -> list[str]:
    """Names of automations that run a filter or group."""
    return [a["name"] for a in list_automations(conn)
            if any(t["type"] == kind and t["id"] == target_id for t in a["runTargets"])]


# ------------------------------------------------------------------ events


def add_event(conn, automation_id: str, type_: str, config_version: int, changes: dict) -> None:
    conn.execute(
        "INSERT INTO automation_events(id, automation_id, type, config_version, changes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("evt"), automation_id, type_, config_version, json.dumps(changes, default=str), now_iso()),
    )


def list_events(conn, automation_id: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM automation_events WHERE automation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (automation_id, limit),
    )
    return [{"id": r["id"], "type": r["type"], "configVersion": r["config_version"],
             "changes": json.loads(r["changes"]), "createdAt": r["created_at"]} for r in rows]


# ------------------------------------------------------------------ schedule state and fires


def get_state(conn, schedule_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM schedule_state WHERE schedule_id = ?", (schedule_id,)).fetchone()
    return dict(r) if r else None


def set_state(conn, automation_id: str, schedule_id: str, last_fired_for: str | None, next_fire_at: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO schedule_state(schedule_id, automation_id, last_fired_for, next_fire_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET last_fired_for = excluded.last_fired_for, "
            "next_fire_at = excluded.next_fire_at",
            (schedule_id, automation_id, last_fired_for, next_fire_at),
        )


def claim_fire(conn, automation_id: str, schedule_id: str, scheduled_for: str) -> str | None:
    """Record that a scheduled time fires. Returns the fire id, or None if it already fired."""
    fire_id = new_id("fire")
    try:
        with conn:
            conn.execute(
                "INSERT INTO schedule_fires(id, automation_id, schedule_id, scheduled_for, fired_at) VALUES (?, ?, ?, ?, ?)",
                (fire_id, automation_id, schedule_id, scheduled_for, now_iso()),
            )
    except sqlite3.IntegrityError:
        return None
    return fire_id


def attach_run(conn, fire_id: str, run_id: str) -> None:
    with conn:
        conn.execute("UPDATE schedule_fires SET run_id = ? WHERE id = ?", (run_id, fire_id))


def fire_count(conn, automation_id: str, schedule_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM schedule_fires WHERE automation_id = ? AND schedule_id = ?",
                        (automation_id, schedule_id)).fetchone()[0]


# ------------------------------------------------------------------ notifications


def notification_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "automationId": r["automation_id"], "runId": r["run_id"], "title": r["title"],
            "message": r["message"], "symbols": json.loads(r["symbols"]), "createdAt": r["created_at"],
            "readAt": r["read_at"]}


def add_notification(conn, automation_id: str, run_id: str | None, title: str, message: str,
                     symbols: list[str]) -> dict:
    nid = new_id("ntf")
    with conn:
        conn.execute(
            "INSERT INTO notifications(id, automation_id, run_id, title, message, symbols, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nid, automation_id, run_id, title, message, json.dumps(symbols), now_iso()),
        )
    return notification_json(conn.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone())


def set_notification_run(conn, notification_id: str, run_id: str) -> None:
    with conn:
        conn.execute("UPDATE notifications SET run_id = ? WHERE id = ?", (run_id, notification_id))


def list_notifications(conn, unread_only: bool = False, limit: int = 100) -> list[dict]:
    where = "WHERE read_at IS NULL" if unread_only else ""
    rows = conn.execute(f"SELECT * FROM notifications {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    return [notification_json(r) for r in rows]


def unread_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]


def mark_read(conn, notification_id: str | None = None) -> int:
    with conn:
        if notification_id:
            cur = conn.execute("UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                               (now_iso(), notification_id))
        else:
            cur = conn.execute("UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (now_iso(),))
    return cur.rowcount


# ------------------------------------------------------------------ global switch


def get_switch(conn) -> dict:
    r = conn.execute("SELECT value FROM app_settings WHERE key = 'automation_switch'").fetchone()
    return json.loads(r["value"]) if r else {"automationsEnabled": True, "changedAt": None, "reason": None}


def set_switch(conn, enabled: bool, reason: str | None) -> dict:
    value = {"automationsEnabled": enabled, "changedAt": now_iso(), "reason": reason}
    with conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES ('automation_switch', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(value),),
        )
    return value
