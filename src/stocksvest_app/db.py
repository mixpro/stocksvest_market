"""SQLite connection and schema setup. Each feature keeps its own tables and queries in its store module."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def iso(dt: datetime) -> str:
    """The one timestamp format stored in the DB: UTC, seconds, 'Z'. Text order = time order
    (ties within a second keep insertion order via rowid)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    from .automations import budget
    from .automations import store as automations_store
    from .brokers import store as brokers_store
    from .filters import store as filters_store
    from .orders import store as orders_store
    from .portfolios import store as portfolios_store
    from .runs import store as runs_store

    for schema in (filters_store.SCHEMA, runs_store.SCHEMA, automations_store.SCHEMA, brokers_store.SCHEMA,
                   portfolios_store.SCHEMA, orders_store.SCHEMA, budget.SCHEMA):
        conn.executescript(schema)
    conn.commit()
