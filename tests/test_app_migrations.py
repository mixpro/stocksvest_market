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


# v2: each automation had its own budget pool in its portfolio (automations.portfolioId).
V2 = """
CREATE TABLE broker_accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, environment TEXT NOT NULL,
    credentials_ref TEXT NOT NULL, base_currency TEXT NOT NULL DEFAULT 'USD', status TEXT NOT NULL, last_error TEXT,
    last_sync_at TEXT, cash TEXT, buying_power TEXT, equity TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE portfolios (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    broker_account_id TEXT NOT NULL REFERENCES broker_accounts(id), currency TEXT NOT NULL,
    allocated TEXT NOT NULL DEFAULT '0', cash TEXT NOT NULL DEFAULT '0', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE portfolio_entries (id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, type TEXT NOT NULL, amount TEXT NOT NULL,
    cash_after TEXT NOT NULL, automation_id TEXT, order_id TEXT, fill_id TEXT, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE position_lots (id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    symbol TEXT NOT NULL, automation_id TEXT, source TEXT NOT NULL, buy_fill_id TEXT, quantity TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL, cost_per_share TEXT NOT NULL, realized_pnl TEXT NOT NULL DEFAULT '0',
    opened_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE orders (id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    broker_account_id TEXT NOT NULL, symbol TEXT NOT NULL, automation_id TEXT, automation_run_id TEXT,
    client_order_id TEXT NOT NULL UNIQUE, broker_order_id TEXT, side TEXT NOT NULL, type TEXT NOT NULL,
    time_in_force TEXT NOT NULL, quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL DEFAULT '0', limit_price TEXT,
    stop_price TEXT, trail_percent TEXT, extended_hours INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
    raw_broker_status TEXT, reject_reason TEXT, average_fill_price TEXT, estimated_price TEXT,
    reserved TEXT NOT NULL DEFAULT '0', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT,
    filled_at TEXT, cancelled_at TEXT);
CREATE TABLE fills (id TEXT PRIMARY KEY, order_id TEXT, portfolio_id TEXT NOT NULL, broker_fill_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL DEFAULT '0',
    executed_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, config_version INTEGER NOT NULL, definition TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE budget_pools (automation_id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, currency TEXT NOT NULL,
    allocated TEXT NOT NULL DEFAULT '0', cash TEXT NOT NULL DEFAULT '0', reserved TEXT NOT NULL DEFAULT '0',
    invested TEXT NOT NULL DEFAULT '0', realized_pnl TEXT NOT NULL DEFAULT '0', fees TEXT NOT NULL DEFAULT '0',
    updated_at TEXT NOT NULL);
CREATE TABLE budget_entries (id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, type TEXT NOT NULL, amount TEXT NOT NULL,
    cash_after TEXT NOT NULL, reserved_after TEXT NOT NULL, run_id TEXT, order_id TEXT, fill_id TEXT, note TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE automation_snapshots (automation_id TEXT NOT NULL, date TEXT NOT NULL, at TEXT NOT NULL,
    portfolio_id TEXT NOT NULL, allocated TEXT NOT NULL, cash TEXT NOT NULL, reserved TEXT NOT NULL,
    invested TEXT NOT NULL, positions_value TEXT NOT NULL, equity TEXT NOT NULL, realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL, fees TEXT NOT NULL, return_percent TEXT, open_positions INTEGER NOT NULL,
    orders_placed INTEGER NOT NULL, fills INTEGER NOT NULL, PRIMARY KEY (automation_id, date));
INSERT INTO broker_accounts VALUES ('bka_1', 'Paper', 'ALPACA', 'PAPER', 'ALPACA', 'USD', 'CONNECTED', NULL, NULL,
    '10000', '20000', '10000', 't', 't');
INSERT INTO portfolios VALUES ('pf_1', 'Paper', '', 'bka_1', 'USD', '10000', '9950', 't', 't');
INSERT INTO automations VALUES ('aut_1', 'Buyer', '', 'ACTIVE', 3,
    '{"portfolioId": "pf_1", "orderGuards": null, "runTargets": [], "schedules": [], "actions": []}', 't', 't');
INSERT INTO budget_pools VALUES ('aut_1', 'pf_1', 'USD', '1000', '950', '0', '50', '0', '0', 't');
INSERT INTO budget_pools VALUES ('aut_gone', 'pf_1', 'USD', '0', '0', '0', '0', '0', '0', 't');
INSERT INTO budget_entries VALUES ('bge_1', 'aut_1', 'ALLOCATE', '1000', '1000', '0', NULL, NULL, NULL, NULL, 't1');
INSERT INTO budget_entries VALUES ('bge_2', 'aut_1', 'BUY_FILL', '-50', '950', '0', NULL, 'ord_1', 'fill_1', NULL, 't2');
INSERT INTO orders(id, portfolio_id, broker_account_id, symbol, automation_id, client_order_id, side, type,
    time_in_force, quantity, status, created_at, updated_at) VALUES ('ord_1', 'pf_1', 'bka_1', 'AAA', 'aut_1', 'c1',
    'BUY', 'MARKET', 'DAY', '5', 'FILLED', 't', 't');
INSERT INTO orders(id, portfolio_id, broker_account_id, symbol, automation_id, client_order_id, side, type,
    time_in_force, quantity, status, created_at, updated_at) VALUES ('ord_2', 'pf_1', 'bka_1', 'BBB', NULL, 'c2',
    'BUY', 'MARKET', 'DAY', '1', 'FILLED', 't', 't');
INSERT INTO position_lots VALUES ('lot_1', 'pf_1', 'AAA', 'aut_1', 'FILL', 'fill_1', '5', '5', '10', '0', 't', NULL);
INSERT INTO automation_snapshots VALUES ('aut_1', '2026-10-16', 't', 'pf_1', '1000', '950', '0', '50', '55', '1005',
    '0', '5', '0', '0.5', 1, 1, 1);
"""


def test_upgrade_automation_pools_to_portfolio_budgets(tmp_path):
    path = tmp_path / "app.db"
    old = sqlite3.connect(path)
    old.executescript(V2)
    old.close()

    conn = db.connect(path)
    db.init(conn)
    db.init(conn)  # running again changes nothing

    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('budget_pools', 'automation_snapshots')").fetchone()
    budgets = {b["name"]: b for b in conn.execute("SELECT * FROM budgets")}
    assert set(budgets) == {"Buyer", "Budget of a deleted automation"}
    b = budgets["Buyer"]
    assert (b["portfolio_id"], b["allocated"], b["cash"], b["invested"]) == ("pf_1", "1000", "950", "50")
    members = conn.execute("SELECT budget_id, automation_id FROM budget_automations").fetchall()
    assert [tuple(m) for m in members] == [(b["id"], "aut_1")]  # the deleted automation's budget has no members

    # Its ledger, orders, shares and history now belong to the budget; manual things stay manual.
    entries = conn.execute("SELECT id, budget_id, automation_id FROM budget_entries ORDER BY id").fetchall()
    assert [tuple(e) for e in entries] == [("bge_1", b["id"], "aut_1"), ("bge_2", b["id"], "aut_1")]
    owners = dict(conn.execute("SELECT id, budget_id FROM orders").fetchall())
    assert owners == {"ord_1": b["id"], "ord_2": None}
    assert conn.execute("SELECT budget_id FROM position_lots").fetchone()[0] == b["id"]
    snap = conn.execute("SELECT budget_id, equity FROM budget_snapshots").fetchone()
    assert tuple(snap) == (b["id"], "1005")

    definition = conn.execute("SELECT definition FROM automations").fetchone()[0]
    assert "portfolioId" not in definition
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
