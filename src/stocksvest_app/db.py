"""SQLite connection and schema setup. Each feature keeps its own tables and queries in its store module."""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def iso(dt: datetime) -> str:
    """The one timestamp format stored in the DB: UTC, seconds, 'Z'. Text order = time order
    (ties within a second keep insertion order via rowid).

    Args:
        dt: a timezone-aware datetime.

    Returns:
        E.g. "2026-10-19T13:30:00Z".

    Used by: now_iso, orders (fills, submitted times), performance.snapshots.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    """The current time in the stored format (see iso). Used by: every store module when stamping rows."""
    return iso(datetime.now(timezone.utc))


def new_id(prefix: str) -> str:
    """A new unique id like "flt_3f2a…".

    Args:
        prefix: names the kind of thing: flt (filter), grp (group), aut (automation), sch (schedule), run, ord
            (order), fill, pf (portfolio), bud (budget), pfe/bge (ledger entries), bka (broker account), ntf (notification),
            evt (event), fire, sync, req (request correlation id), sv (client order id).

    Used by: every store module, the audit middleware, the runner and order placement.
    """
    return f"{prefix}_{uuid.uuid4().hex}"


def connect(path: Path) -> sqlite3.Connection:
    """Open the app database: WAL mode (readers don't block the writer), foreign keys on, 30 s lock wait.

    Args:
        path: database file; its folder is created if missing.

    Returns:
        A connection with sqlite3.Row rows, usable from several threads.

    Used by: deps.AppContext.conn (per request), api lifespan, the scheduler thread, audit.store.record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection):
    """A write transaction that takes the database's write lock before reading anything. Use it around
    "check a balance, then change it" so two requests can't both pass the check (they queue instead).

    Args:
        conn: the connection to run the transaction on; an open implicit transaction is committed first.

    Yields:
        The same connection. Committed on success, rolled back on any exception.

    Used by: portfolio capital (create/allocate/withdraw), budget allocate/withdraw/close, order placement
    (_check_funds + reserve), fill import in sync_account, deleting an automation.
    """
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
    """Create every feature's tables, add later columns, and run upgrades (idempotent).

    Args:
        conn: an open connection.

    Used by: api.create_app (startup), tests.
    """
    from .audit import store as audit_store
    from .automations import store as automations_store
    from .budgets import store as budgets_store
    from .brokers import store as brokers_store
    from .filters import store as filters_store
    from .orders import store as orders_store
    from .performance import store as performance_store
    from .portfolios import store as portfolios_store
    from .runs import store as runs_store

    for schema in (filters_store.SCHEMA, runs_store.SCHEMA, automations_store.SCHEMA, brokers_store.SCHEMA,
                   portfolios_store.SCHEMA, orders_store.SCHEMA, budgets_store.SCHEMA, performance_store.SCHEMA,
                   audit_store.SCHEMA):
        conn.executescript(schema)
    for table, columns in {**ADDED_COLUMNS, "broker_accounts": brokers_store.ADDED_COLUMNS}.items():
        existing = _columns(conn, table)
        for name, kind in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()
    migrate(conn)
    conn.executescript(INDEXES)
    conn.commit()


# Columns added after the first release, per table (also in the modules' SCHEMA for new databases).
# Used by: init.
ADDED_COLUMNS = {
    "orders": {"budget_id": "TEXT"},
    "fills": {"realized_pnl": "TEXT"},
    "position_lots": {"budget_id": "TEXT"},
    "portfolio_entries": {"budget_id": "TEXT"},
}
# Indexes on added or upgraded columns: created after the columns exist and migrate has run. Used by: init.
INDEXES = """
CREATE INDEX IF NOT EXISTS budget_entries_by_budget ON budget_entries(budget_id, created_at);
CREATE INDEX IF NOT EXISTS orders_by_budget ON orders(budget_id, status);
CREATE INDEX IF NOT EXISTS lots_by_budget ON position_lots(portfolio_id, symbol, budget_id, closed_at);
"""


def _columns(conn, table: str) -> set[str]:
    """Column names of a table.

    Args:
        conn: an open connection.
        table: table name.

    Used by: migrate.
    """
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _create_statement(schema: str, table: str, new_name: str) -> str:
    """Take a table's CREATE statement out of a module SCHEMA, renamed (used to rebuild tables).

    Args:
        schema: a module's SCHEMA text.
        table: the table whose statement to take.
        new_name: name for the new table.

    Used by: _rebuild.
    """
    start = schema.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = schema.index(";", start)
    return schema[start:end].replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {new_name} (", 1)


def _rebuild(conn, table: str, schema: str, copy_sql: str, after=None) -> None:
    """SQLite's safe table rebuild: create new, copy, drop old, rename new (references from other tables
    keep pointing at the table name, so they follow the new table). Foreign keys are off meanwhile.

    Args:
        conn: an open connection.
        table: the table to rebuild.
        schema: the owning module's SCHEMA (holds the new CREATE statement).
        copy_sql: INSERT ... SELECT from the old table; "{new}" is replaced by the new table's name.
        after: optional callable run after the copy, before the old table is dropped.

    Used by: migrate.
    """
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
    """Upgrade databases created by earlier versions. Each step checks the old layout, so it runs once.

    Steps: v2 portfolios with their own capital (an old portfolio keeps the whole account as allocated capital,
    with an ALLOCATE ledger entry); v2 broker sync records per account instead of per portfolio; v3 per-automation
    budget pools become portfolio budgets (see _budgets_from_pools).

    Args:
        conn: an open connection.

    Used by: init.
    """
    from decimal import Decimal

    from .orders.store import SCHEMA as orders_schema
    from .portfolios.store import SCHEMA as portfolios_schema

    from .audit.store import record

    # v2: several portfolios per broker account, each with its own capital and a ledger.
    if "buying_power" in _columns(conn, "portfolios"):
        record(conn, "DB_UPGRADE", details={"step": "portfolios get their own capital"}, source="SYSTEM")
        old = conn.execute("SELECT id, cash FROM portfolios").fetchall()

        def opening_capital():  # each old portfolio owned its whole account: keep that as allocated capital
            """Give each upgraded portfolio its old cash as allocated capital, with a ledger entry."""
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

    # v3: budgets belong to portfolios and are shared by automations.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'budget_pools'").fetchone():
        _budgets_from_pools(conn)


def _budgets_from_pools(conn: sqlite3.Connection) -> None:
    """v3 upgrade: each automation's budget pool becomes a budget of its portfolio, named after the automation,
    with the automation attached. Its balances, ledger, orders, shares and daily snapshots move to the budget.
    The automations' portfolioId setting is dropped (the portfolio now chooses its automations).

    Args:
        conn: an open connection.

    Used by: migrate.
    """
    import json

    from .audit.store import record
    from .budgets.store import SCHEMA as budgets_schema

    pools = conn.execute("SELECT p.*, a.name AS automation_name FROM budget_pools p "
                         "LEFT JOIN automations a ON a.id = p.automation_id").fetchall()
    record(conn, "DB_UPGRADE", details={"step": "automation budgets become portfolio budgets", "pools": len(pools)},
           source="SYSTEM")
    ledger_has_budget = "budget_id" in _columns(conn, "budget_entries")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute("CREATE TEMP TABLE pool_budget (automation_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL)")
            for p in pools:
                bid, base = new_id("bud"), p["automation_name"] or "Budget of a deleted automation"
                name, n = base, 2
                while conn.execute("SELECT 1 FROM budgets WHERE portfolio_id = ? AND name = ?",
                                   (p["portfolio_id"], name)).fetchone():
                    name, n = f"{base} ({n})", n + 1
                conn.execute(
                    "INSERT INTO budgets(id, portfolio_id, name, currency, allocated, cash, reserved, invested, "
                    "realized_pnl, fees, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (bid, p["portfolio_id"], name, p["currency"], p["allocated"], p["cash"], p["reserved"],
                     p["invested"], p["realized_pnl"], p["fees"], p["updated_at"], p["updated_at"]))
                if p["automation_name"] is not None:
                    conn.execute("INSERT INTO budget_automations(budget_id, automation_id, added_at) VALUES (?, ?, ?)",
                                 (bid, p["automation_id"], now_iso()))
                conn.execute("INSERT INTO pool_budget VALUES (?, ?)", (p["automation_id"], bid))
            owner = "(SELECT budget_id FROM pool_budget m WHERE m.automation_id = {t}.automation_id)"
            for table in ("orders", "position_lots", "portfolio_entries"):
                conn.execute(f"UPDATE {table} SET budget_id = {owner.format(t=table)} "
                             f"WHERE automation_id IS NOT NULL AND budget_id IS NULL")
            if not ledger_has_budget:  # the old ledger was keyed by automation: rebuild it keyed by budget
                conn.execute(_create_statement(budgets_schema, "budget_entries", "budget_entries_new"))
                conn.execute(
                    "INSERT INTO budget_entries_new(id, budget_id, automation_id, type, amount, cash_after, "
                    "reserved_after, run_id, order_id, fill_id, note, created_at) "
                    "SELECT e.id, m.budget_id, e.automation_id, e.type, e.amount, e.cash_after, e.reserved_after, "
                    "e.run_id, e.order_id, e.fill_id, e.note, e.created_at "
                    "FROM budget_entries e JOIN pool_budget m ON m.automation_id = e.automation_id")
                conn.execute("DROP TABLE budget_entries")
                conn.execute("ALTER TABLE budget_entries_new RENAME TO budget_entries")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'automation_snapshots'").fetchone():
                conn.execute(
                    "INSERT OR IGNORE INTO budget_snapshots(budget_id, date, at, portfolio_id, allocated, cash, reserved, "
                    "invested, positions_value, equity, realized_pnl, unrealized_pnl, fees, return_percent, "
                    "open_positions, orders_placed, fills) "
                    "SELECT m.budget_id, s.date, s.at, s.portfolio_id, s.allocated, s.cash, s.reserved, s.invested, "
                    "s.positions_value, s.equity, s.realized_pnl, s.unrealized_pnl, s.fees, s.return_percent, "
                    "s.open_positions, s.orders_placed, s.fills "
                    "FROM automation_snapshots s JOIN pool_budget m ON m.automation_id = s.automation_id")
                conn.execute("DROP TABLE automation_snapshots")
            for a in conn.execute("SELECT id, definition FROM automations").fetchall():
                definition = json.loads(a["definition"])
                if "portfolioId" in definition:
                    definition.pop("portfolioId")
                    conn.execute("UPDATE automations SET definition = ? WHERE id = ?", (json.dumps(definition), a["id"]))
            conn.execute("DROP TABLE budget_pools")
            conn.execute("DROP TABLE pool_budget")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
