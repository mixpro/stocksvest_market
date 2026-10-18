"""SQLite connection and schema setup. Each feature keeps its own tables and queries in its store module."""

import sqlite3
import uuid
from contextlib import contextmanager
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


@contextmanager
def immediate(conn: sqlite3.Connection):
    """A write transaction that takes the database's write lock before reading anything. Use it around
    "check a balance, then change it" so two requests can't both pass the check (they queue instead)."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def init(conn: sqlite3.Connection) -> None:
    from .audit import store as audit_store
    from .automations import budget
    from .automations import store as automations_store
    from .brokers import store as brokers_store
    from .filters import store as filters_store
    from .orders import store as orders_store
    from .performance import store as performance_store
    from .portfolios import store as portfolios_store
    from .runs import store as runs_store

    for schema in (filters_store.SCHEMA, runs_store.SCHEMA, automations_store.SCHEMA, brokers_store.SCHEMA,
                   portfolios_store.SCHEMA, orders_store.SCHEMA, budget.SCHEMA, performance_store.SCHEMA,
                   audit_store.SCHEMA):
        conn.executescript(schema)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(broker_accounts)")}
    for name, kind in brokers_store.ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE broker_accounts ADD COLUMN {name} {kind}")
    conn.commit()
    migrate(conn)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _create_statement(schema: str, table: str, new_name: str) -> str:
    start = schema.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = schema.index(";", start)
    return schema[start:end].replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {new_name} (", 1)


def _rebuild(conn, table: str, schema: str, copy_sql: str, after=None) -> None:
    """SQLite's safe table rebuild: create new, copy, drop old, rename new (references from other tables
    keep pointing at the table name, so they follow the new table)."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute(_create_statement(schema, table, f"{table}_new"))
            conn.execute(copy_sql.format(new=f"{table}_new"))
            if after:
                after()
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by earlier versions. Each step checks the old layout, so it runs once."""
    from decimal import Decimal

    from .orders.store import SCHEMA as orders_schema
    from .portfolios.store import SCHEMA as portfolios_schema

    from .audit.store import record

    # v2: several portfolios per broker account, each with its own capital and a ledger.
    if "buying_power" in _columns(conn, "portfolios"):
        record(conn, "DB_UPGRADE", details={"step": "portfolios get their own capital"}, source="SYSTEM")
        old = conn.execute("SELECT id, cash FROM portfolios").fetchall()

        def opening_capital():  # each old portfolio owned its whole account: keep that as allocated capital
            for r in old:
                amount = Decimal(r["cash"] or "0")
                if amount > 0:
                    conn.execute("UPDATE portfolios_new SET allocated = ?, cash = ? WHERE id = ?",
                                 (str(amount), str(amount), r["id"]))
                    conn.execute(
                        "INSERT INTO portfolio_entries(id, portfolio_id, type, amount, cash_after, note, created_at) "
                        "VALUES (?, ?, 'ALLOCATE', ?, ?, ?, ?)",
                        (new_id("pfe"), r["id"], str(amount), str(amount),
                         "upgrade: the portfolio held the whole account", now_iso()))

        _rebuild(conn, "portfolios", portfolios_schema,
                 "INSERT INTO {new}(id, name, description, broker_account_id, currency, created_at, updated_at) "
                 "SELECT id, name, description, broker_account_id, currency, created_at, updated_at FROM portfolios",
                 opening_capital)

    # v2: sync records per broker account instead of per portfolio.
    if "portfolio_id" in _columns(conn, "broker_syncs"):
        _rebuild(conn, "broker_syncs", orders_schema,
                 "INSERT INTO {new}(id, broker_account_id, ran_at, orders_updated, fills_imported, mismatches, error) "
                 "SELECT id, broker_account_id, ran_at, orders_updated, fills_imported, mismatches, error "
                 "FROM broker_syncs")
        with conn:
            conn.execute("CREATE INDEX IF NOT EXISTS broker_syncs_by_account ON broker_syncs(broker_account_id, ran_at)")
