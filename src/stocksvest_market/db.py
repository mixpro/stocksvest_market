"""SQLite storage. One writer (the sync), many readers (the API) via WAL mode."""

import sqlite3
from pathlib import Path

# Stored bar timeframes -> their table. Week/month/year are aggregated from 1Day on read.
# Used by: sync (sync_bars, prune, run_incremental), api (load_bars, status, latest), aggregate callers.
BAR_TABLES = {"1Min": "bars_1min", "1Hour": "bars_1hour", "1Day": "bars_1day"}
# Real-time minute bars from IBKR for watched symbols, newer than Alpaca's delayed SIP data allows. Kept apart so
# the Alpaca sync (which resumes from each symbol's newest bar in bars_1min) still fetches the full SIP bars.
# Used by: init, ibkr.IbkrQuotes (writes), api.latest (reads).
LIVE_TABLE = "bars_live_1min"

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id          TEXT PRIMARY KEY,          -- Alpaca asset id: stable across ticker renames
    ticker      TEXT NOT NULL UNIQUE,      -- Alpaca format, e.g. BRK.B
    name        TEXT NOT NULL,
    exchange    TEXT NOT NULL,             -- NASDAQ | NYSE
    asset_type  TEXT NOT NULL,             -- COMMON_STOCK | ADR
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# Columns added after the first release; added to existing databases by init().
SYMBOL_COLUMNS = {
    "cik": "INTEGER",
    "sic": "INTEGER",
    "sic_description": "TEXT",
    "sector": "TEXT",  # key of sectors.SECTORS, NULL until looked up
}

BAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    symbol_id TEXT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    ts        INTEGER NOT NULL,            -- bar start, unix seconds UTC
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    v REAL NOT NULL,
    n INTEGER,                             -- trade count
    vw REAL,                               -- volume-weighted average price
    PRIMARY KEY (symbol_id, ts)
) WITHOUT ROWID;
"""

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar (
    date          TEXT PRIMARY KEY,        -- YYYY-MM-DD, trading days only
    session_open  TEXT NOT NULL,           -- ISO datetime, extended session start (ET)
    open          TEXT NOT NULL,           -- regular session open (ET)
    close         TEXT NOT NULL,           -- regular session close (ET)
    session_close TEXT NOT NULL            -- extended session end (ET)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Symbols that left the universe and why (NON_COMPLIANT, NOT_TRADABLE, NOT_IN_HALAL_DB, PENDING, ...). A symbol
-- that comes back is taken out. Lets the app sell or flag shares it still holds.
CREATE TABLE IF NOT EXISTS removed_symbols (
    ticker      TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    removed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halal_imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at     TEXT NOT NULL,
    compliant_count INTEGER NOT NULL,
    added           INTEGER NOT NULL,
    removed         INTEGER NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the market database with the settings every connection needs.

    WAL mode lets the API read while the sync writes; foreign keys make deleting a symbol delete its bars.

    Args:
        path: database file; its folder is created if missing.

    Returns:
        A connection with sqlite3.Row rows, usable from several threads.

    Used by: api (lifespan, get_conn, background jobs), scheduler.Scheduler._loop, __main__.main,
    halal.compliant_tickers (read-only variant).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create missing tables and add columns introduced after the first release (idempotent).

    Args:
        conn: an open connection (see connect).

    Used by: api.create_app (startup), __main__.main.
    """
    conn.executescript(SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(symbols)")}
    for name, kind in SYMBOL_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE symbols ADD COLUMN {name} {kind}")
    for table in (*BAR_TABLES.values(), LIVE_TABLE):
        conn.executescript(BAR_SCHEMA.format(table=table))
    conn.executescript(EXTRA_SCHEMA)
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a sync bookkeeping value.

    Args:
        conn: an open connection.
        key: one of "last_incremental_at", "last_nightly_at", "last_nightly_date".

    Returns:
        The stored string, or None if never set.

    Used by: api.status, scheduler.due_jobs.
    """
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Store a sync bookkeeping value (insert or replace) and commit.

    Args:
        conn: an open connection.
        key: see get_state.
        value: ISO timestamp or date.

    Used by: sync.run_incremental, sync.run_nightly.
    """
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
