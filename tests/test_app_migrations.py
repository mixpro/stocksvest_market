"""Upgrading databases created by earlier app versions."""

import sqlite3

from stocksvest_app import db

V1 = """
CREATE TABLE broker_accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, environment TEXT NOT NULL,
    credentials_ref TEXT NOT NULL, base_currency TEXT NOT NULL DEFAULT 'USD', status TEXT NOT NULL, last_error TEXT,
    last_sync_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE portfolios (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    broker_account_id TEXT NOT NULL UNIQUE REFERENCES broker_accounts(id), currency TEXT NOT NULL,
    cash TEXT NOT NULL DEFAULT '0', buying_power TEXT NOT NULL DEFAULT '0', equity TEXT NOT NULL DEFAULT '0',
    cash_synced_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE position_lots (id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    symbol TEXT NOT NULL, automation_id TEXT, source TEXT NOT NULL, buy_fill_id TEXT, quantity TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL, cost_per_share TEXT NOT NULL, realized_pnl TEXT NOT NULL DEFAULT '0',
    opened_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE broker_syncs (id TEXT PRIMARY KEY, broker_account_id TEXT NOT NULL, portfolio_id TEXT NOT NULL,
    ran_at TEXT NOT NULL, orders_updated INTEGER NOT NULL, fills_imported INTEGER NOT NULL, mismatches TEXT NOT NULL,
    error TEXT);
INSERT INTO broker_accounts VALUES ('bka_1', 'Paper', 'ALPACA', 'PAPER', 'ALPACA', 'USD', 'CONNECTED', NULL, NULL,
    '2026-10-17T00:00:00Z', '2026-10-17T00:00:00Z');
INSERT INTO portfolios VALUES ('pf_1', 'Paper', '', 'bka_1', 'USD', '100000', '400000', '100000', NULL,
    '2026-10-17T00:00:00Z', '2026-10-17T00:00:00Z');
INSERT INTO position_lots VALUES ('lot_1', 'pf_1', 'AAA', NULL, 'FILL', NULL, '5', '5', '10', '0',
    '2026-10-17T00:00:00Z', NULL);
INSERT INTO broker_syncs VALUES ('sync_1', 'bka_1', 'pf_1', '2026-10-17T00:00:00Z', 0, 1, '[]', NULL);
"""


def test_upgrade_from_one_portfolio_per_account(tmp_path):
    path = tmp_path / "app.db"
    old = sqlite3.connect(path)
    old.executescript(V1)
    old.close()

    conn = db.connect(path)
    db.init(conn)
    db.init(conn)  # running again changes nothing

    p = conn.execute("SELECT * FROM portfolios").fetchone()
    assert (p["id"], p["allocated"], p["cash"]) == ("pf_1", "100000", "100000")
    assert "buying_power" not in {r[1] for r in conn.execute("PRAGMA table_info(portfolios)")}
    entries = conn.execute("SELECT type, amount, cash_after FROM portfolio_entries").fetchall()
    assert [tuple(e) for e in entries] == [("ALLOCATE", "100000", "100000")]

    # Several portfolios per account are now allowed, and lots still point at portfolios.
    conn.execute("INSERT INTO portfolios(id, name, broker_account_id, currency, created_at, updated_at) "
                 "VALUES ('pf_2', 'Second', 'bka_1', 'USD', 'x', 'x')")
    fk = conn.execute("PRAGMA foreign_key_list(position_lots)").fetchone()
    assert fk["table"] == "portfolios"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    sync = conn.execute("SELECT * FROM broker_syncs").fetchone()
    assert (sync["id"], sync["fills_imported"], sync["unassigned"]) == ("sync_1", 1, "{}")
    assert {"cash", "equity"} <= {r[1] for r in conn.execute("PRAGMA table_info(broker_accounts)")}
