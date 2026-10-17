"""Filters and filter groups tables and queries."""

import json
import sqlite3

from ..db import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS filters (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1,
    inputs      TEXT NOT NULL,             -- JSON list of {type, id}
    actions     TEXT NOT NULL,             -- JSON list of actions, applied in order
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filter_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filter_group_members (
    group_id  TEXT NOT NULL REFERENCES filter_groups(id) ON DELETE CASCADE,
    filter_id TEXT NOT NULL REFERENCES filters(id),
    position  INTEGER NOT NULL,
    PRIMARY KEY (group_id, filter_id)
);
"""


# ------------------------------------------------------------------ filters


def filter_json(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"],
        "isActive": bool(r["is_active"]),
        "inputs": json.loads(r["inputs"]),
        "actions": json.loads(r["actions"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def get_filter(conn, filter_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM filters WHERE id = ?", (filter_id,)).fetchone()
    return filter_json(r) if r else None


def list_filters(conn) -> list[dict]:
    return [filter_json(r) for r in conn.execute("SELECT * FROM filters ORDER BY name")]


def save_filter(conn, data: dict, filter_id: str | None = None) -> dict:
    ts = now_iso()
    args = (data["name"], data["description"], int(data["isActive"]),
            json.dumps(data["inputs"]), json.dumps(data["actions"]), ts)
    with conn:
        if filter_id is None:
            filter_id = new_id("flt")
            conn.execute(
                "INSERT INTO filters(name, description, is_active, inputs, actions, updated_at, id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                args + (filter_id, ts),
            )
        else:
            conn.execute(
                "UPDATE filters SET name = ?, description = ?, is_active = ?, inputs = ?, actions = ?, updated_at = ? "
                "WHERE id = ?",
                args + (filter_id,),
            )
    return get_filter(conn, filter_id)


def delete_filter(conn, filter_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM filters WHERE id = ?", (filter_id,))


def filter_users(conn, filter_id: str) -> list[str]:
    """Names of filters and groups that use this filter."""
    users = [f["name"] for f in list_filters(conn) if any(i.get("id") == filter_id for i in f["inputs"])]
    users += [r["name"] for r in conn.execute(
        "SELECT g.name FROM filter_groups g JOIN filter_group_members m ON m.group_id = g.id WHERE m.filter_id = ?",
        (filter_id,),
    )]
    return users


# ------------------------------------------------------------------ groups


def get_group(conn, group_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM filter_groups WHERE id = ?", (group_id,)).fetchone()
    if not r:
        return None
    members = [m["filter_id"] for m in conn.execute(
        "SELECT filter_id FROM filter_group_members WHERE group_id = ? ORDER BY position", (group_id,))]
    return {"id": r["id"], "name": r["name"], "description": r["description"], "filterIds": members,
            "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


def list_groups(conn) -> list[dict]:
    return [get_group(conn, r["id"]) for r in conn.execute("SELECT id FROM filter_groups ORDER BY name")]


def save_group(conn, data: dict, group_id: str | None = None) -> dict:
    ts = now_iso()
    with conn:
        if group_id is None:
            group_id = new_id("grp")
            conn.execute(
                "INSERT INTO filter_groups(id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (group_id, data["name"], data["description"], ts, ts),
            )
        else:
            conn.execute("UPDATE filter_groups SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                         (data["name"], data["description"], ts, group_id))
            conn.execute("DELETE FROM filter_group_members WHERE group_id = ?", (group_id,))
        conn.executemany(
            "INSERT INTO filter_group_members(group_id, filter_id, position) VALUES (?, ?, ?)",
            [(group_id, fid, i) for i, fid in enumerate(data["filterIds"])],
        )
    return get_group(conn, group_id)


def delete_group(conn, group_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM filter_groups WHERE id = ?", (group_id,))


def group_users(conn, group_id: str) -> list[str]:
    return [f["name"] for f in list_filters(conn) if any(i.get("id") == group_id for i in f["inputs"])]
