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
    type           TEXT NOT NULL,              -- CREATED UPDATED PAUSED RESUMED DELETED BUDGET_ADDED BUDGET_REMOVED
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
    """An automations row -> API shape: id, name, description, status, configVersion, the definition fields
    (orderGuards, runTargets, schedules, actions), createdAt, updatedAt.

    Args:
        r: row from the automations table.

    Used by: get_automation, list_automations.
    """
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"],
        "status": r["status"],
        "configVersion": r["config_version"],
        "orderGuards": None,
        **json.loads(r["definition"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def get_automation(conn, automation_id: str) -> dict | None:
    """One automation, or None.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.

    Used by: routes, budgets.routes.
    """
    r = conn.execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
    return automation_json(r) if r else None


def list_automations(conn, status: str | None = None) -> list[dict]:
    """Automations by name.

    Args:
        conn: app DB connection.
        status: "ACTIVE" or "PAUSED"; None = all.

    Used by: routes.list_automations, scheduler.tick (ACTIVE only), automations_using, portfolios.routes.
    """
    if status:
        rows = conn.execute("SELECT * FROM automations WHERE status = ? ORDER BY name", (status,))
    else:
        rows = conn.execute("SELECT * FROM automations ORDER BY name")
    return [automation_json(r) for r in rows]


def _definition(data: dict) -> str:
    """The stored JSON definition (orderGuards, runTargets, schedules, actions) of a validated body.

    Args:
        data: AutomationIn as dict.

    Used by: create_automation, update_automation.
    """
    return json.dumps({k: data.get(k) for k in ("orderGuards", "runTargets", "schedules", "actions")})


def create_automation(conn, data: dict) -> dict:
    """Create an ACTIVE automation at configVersion 1 and record a CREATED event.

    Args:
        conn: app DB connection.
        data: AutomationIn as dict, schedules with ids (routes.prepare).

    Used by: routes.create_automation, tests.
    """
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
    """Replace the definition, bump configVersion, reset schedule state (schedules restart from now) and record
    an UPDATED event listing what changed.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        data: AutomationIn as dict.

    Used by: routes.update_automation.
    """
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
    """Pause or resume. Schedule state is reset, so times missed while paused are not replayed.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        status: "ACTIVE" or "PAUSED".

    Used by: routes.pause, routes.resume.
    """
    a = get_automation(conn, automation_id)
    with conn:
        conn.execute("UPDATE automations SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), automation_id))
        # Resuming starts from now: times missed while paused are not replayed.
        conn.execute("DELETE FROM schedule_state WHERE automation_id = ?", (automation_id,))
        add_event(conn, automation_id, "PAUSED" if status == "PAUSED" else "RESUMED", a["configVersion"], {})
    return get_automation(conn, automation_id)


def delete_automation(conn, automation_id: str) -> None:
    """Delete an automation and take it out of its budgets (its runs, events and the budgets' ledgers stay);
    records a DELETED event.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.

    Used by: routes.delete_automation (after its guards).
    """
    a = get_automation(conn, automation_id)
    with conn:
        conn.execute("DELETE FROM budget_automations WHERE automation_id = ?", (automation_id,))
        conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
        add_event(conn, automation_id, "DELETED", a["configVersion"], {"name": a["name"]})


def automations_using(conn, kind: str, target_id: str) -> list[str]:
    """Names of automations that run a filter or group.

    Args:
        conn: app DB connection.
        kind: "FILTER" or "FILTER_GROUP".
        target_id: the filter or group id.

    Used by: filters.routes (refuse deleting what an automation uses).
    """
    return [a["name"] for a in list_automations(conn)
            if any(t["type"] == kind and t["id"] == target_id for t in a["runTargets"])]


# ------------------------------------------------------------------ events


def add_event(conn, automation_id: str, type_: str, config_version: int, changes: dict) -> None:
    """Record an automation change in its change log (caller commits).

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        type_: CREATED, UPDATED, PAUSED, RESUMED, DELETED, BUDGET_ALLOCATED or BUDGET_WITHDRAWN.
        config_version: the version after the change.
        changes: what changed ({field: {from, to}} for UPDATED, the budget for budget events).

    Used by: create/update/set_status/delete_automation, budgets.routes (added to / removed from a budget).
    """
    conn.execute(
        "INSERT INTO automation_events(id, automation_id, type, config_version, changes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("evt"), automation_id, type_, config_version, json.dumps(changes, default=str), now_iso()),
    )


def list_events(conn, automation_id: str, limit: int = 100) -> list[dict]:
    """An automation's change log, newest first.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        limit: maximum events.

    Used by: routes.events.
    """
    rows = conn.execute(
        "SELECT * FROM automation_events WHERE automation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (automation_id, limit),
    )
    return [{"id": r["id"], "type": r["type"], "configVersion": r["config_version"],
             "changes": json.loads(r["changes"]), "createdAt": r["created_at"]} for r in rows]


# ------------------------------------------------------------------ schedule state and fires


def get_state(conn, schedule_id: str) -> dict | None:
    """A schedule's scheduler state {schedule_id, automation_id, last_fired_for, next_fire_at}, or None when it
    hasn't been planned yet (new, edited or resumed).

    Args:
        conn: app DB connection.
        schedule_id: the schedule's id.

    Used by: scheduler._tick_schedule, routes.with_status.
    """
    r = conn.execute("SELECT * FROM schedule_state WHERE schedule_id = ?", (schedule_id,)).fetchone()
    return dict(r) if r else None


def set_state(conn, automation_id: str, schedule_id: str, last_fired_for: str | None, next_fire_at: str | None) -> None:
    """Store a schedule's scheduler state (insert or update).

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        schedule_id: the schedule's id.
        last_fired_for: the last scheduled time handled (fired or skipped), ISO; None if none yet.
        next_fire_at: the next time due, ISO; None when the schedule has ended.

    Used by: scheduler._tick_schedule.
    """
    with conn:
        conn.execute(
            "INSERT INTO schedule_state(schedule_id, automation_id, last_fired_for, next_fire_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET last_fired_for = excluded.last_fired_for, "
            "next_fire_at = excluded.next_fire_at",
            (schedule_id, automation_id, last_fired_for, next_fire_at),
        )


def claim_fire(conn, automation_id: str, schedule_id: str, scheduled_for: str) -> str | None:
    """Record that a scheduled time fires. The (automation, schedule, time) unique key makes this the guarantee
    that a time never runs twice, even across restarts or two scheduler processes.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        schedule_id: the schedule's id.
        scheduled_for: the scheduled time, ISO UTC.

    Returns:
        The new fire id, or None if this time already fired.

    Used by: scheduler._tick_schedule.
    """
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
    """Link a fire record to the run it produced.

    Args:
        conn: app DB connection.
        fire_id: from claim_fire.
        run_id: the stored run.

    Used by: runner.run_automation.
    """
    with conn:
        conn.execute("UPDATE schedule_fires SET run_id = ? WHERE id = ?", (run_id, fire_id))


def fire_count(conn, automation_id: str, schedule_id: str) -> int:
    """How many times a schedule has fired (for end COUNT).

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        schedule_id: the schedule's id.

    Used by: scheduler._tick_schedule, routes (next runs).
    """
    return conn.execute("SELECT COUNT(*) FROM schedule_fires WHERE automation_id = ? AND schedule_id = ?",
                        (automation_id, schedule_id)).fetchone()[0]


# ------------------------------------------------------------------ notifications


def notification_json(r: sqlite3.Row) -> dict:
    """A notifications row -> API shape {id, automationId, runId, title, message, symbols, createdAt, readAt}.

    Args:
        r: row from the notifications table.
    """
    return {"id": r["id"], "automationId": r["automation_id"], "runId": r["run_id"], "title": r["title"],
            "message": r["message"], "symbols": json.loads(r["symbols"]), "createdAt": r["created_at"],
            "readAt": r["read_at"]}


def add_notification(conn, automation_id: str, run_id: str | None, title: str, message: str,
                     symbols: list[str]) -> dict:
    """Put a notification in the inbox.

    Args:
        conn: app DB connection.
        automation_id: the automation's id.
        run_id: the run that sent it (set later with set_notification_run when not known yet).
        title: rendered title.
        message: rendered message.
        symbols: matched tickers to show (may be empty).

    Used by: runner.run_automation (NOTIFY action).
    """
    nid = new_id("ntf")
    with conn:
        conn.execute(
            "INSERT INTO notifications(id, automation_id, run_id, title, message, symbols, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nid, automation_id, run_id, title, message, json.dumps(symbols), now_iso()),
        )
    return notification_json(conn.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone())


def set_notification_run(conn, notification_id: str, run_id: str) -> None:
    """Link a notification to its run once the run is stored.

    Args:
        conn: app DB connection.
        notification_id: the notification's id.
        run_id: the run's id.

    Used by: runner.run_automation.
    """
    with conn:
        conn.execute("UPDATE notifications SET run_id = ? WHERE id = ?", (run_id, notification_id))


def list_notifications(conn, unread_only: bool = False, limit: int = 100) -> list[dict]:
    """Notifications, newest first.

    Args:
        conn: app DB connection.
        unread_only: only unread ones.
        limit: maximum number.

    Used by: routes.notifications.
    """
    where = "WHERE read_at IS NULL" if unread_only else ""
    rows = conn.execute(f"SELECT * FROM notifications {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    return [notification_json(r) for r in rows]


def unread_count(conn) -> int:
    """Number of unread notifications.

    Args:
        conn: app DB connection.

    Used by: routes.notifications (and the UI bell).
    """
    return conn.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]


def mark_read(conn, notification_id: str | None = None) -> int:
    """Mark one notification (or all unread ones) as read.

    Args:
        conn: app DB connection.
        notification_id: the notification; None = all unread.

    Returns:
        How many were marked.

    Used by: routes.read_one, routes.read_all.
    """
    with conn:
        if notification_id:
            cur = conn.execute("UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                               (now_iso(), notification_id))
        else:
            cur = conn.execute("UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (now_iso(),))
    return cur.rowcount


# ------------------------------------------------------------------ global switch


def get_switch(conn) -> dict:
    """The global switch {automationsEnabled, changedAt, reason} (on by default).

    Args:
        conn: app DB connection.

    Used by: runner.run_automation (SWITCH_OFF skips), routes.get_switch.
    """
    r = conn.execute("SELECT value FROM app_settings WHERE key = 'automation_switch'").fetchone()
    return json.loads(r["value"]) if r else {"automationsEnabled": True, "changedAt": None, "reason": None}


def set_switch(conn, enabled: bool, reason: str | None) -> dict:
    """Turn scheduled runs on or off for every automation.

    Args:
        conn: app DB connection.
        enabled: True = scheduled runs happen.
        reason: why (shown in the UI and the response).

    Used by: routes.set_switch.
    """
    value = {"automationsEnabled": enabled, "changedAt": now_iso(), "reason": reason}
    with conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES ('automation_switch', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(value),),
        )
    return value
