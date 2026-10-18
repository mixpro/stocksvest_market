"""Broker accounts. Credentials are never stored: `credentials_ref` names environment variables
(<REF>_KEY and <REF>_SECRET in .env)."""

import sqlite3

from ..db import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_accounts (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    provider        TEXT NOT NULL,             -- ALPACA | IBKR
    environment     TEXT NOT NULL,             -- PAPER | LIVE
    credentials_ref TEXT NOT NULL,
    base_currency   TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL,             -- CONNECTED | ERROR | DISABLED
    last_error      TEXT,
    last_sync_at    TEXT,
    cash            TEXT,                      -- balances from the last sync
    buying_power    TEXT,
    equity          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


# Columns added after the first release; added to existing databases by db.init(). Used by: db.init
ADDED_COLUMNS = {"cash": "TEXT", "buying_power": "TEXT", "equity": "TEXT"}


def _num(v):
    """A stored decimal string as float, or None.

    Args:
        v: the value.
    """
    return float(v) if v is not None else None


def account_json(r: sqlite3.Row) -> dict:
    """A broker_accounts row in API form (camelCase; balances as numbers).

    Args:
        r: the row.

    Used by: get_account, list_accounts.
    """
    return {"id": r["id"], "name": r["name"], "provider": r["provider"], "environment": r["environment"],
            "credentialsRef": r["credentials_ref"], "baseCurrency": r["base_currency"], "status": r["status"],
            "lastError": r["last_error"], "lastSyncAt": r["last_sync_at"], "cash": _num(r["cash"]),
            "buyingPower": _num(r["buying_power"]), "equity": _num(r["equity"]), "createdAt": r["created_at"],
            "updatedAt": r["updated_at"]}


def set_balances(conn, account_id: str, cash, buying_power, equity) -> None:
    """Store the balances from the last sync.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.
        cash: cash balance.
        buying_power: buying power.
        equity: equity.

    Used by: brokers.routes.check, orders.service.sync_account.
    """
    with conn:
        conn.execute("UPDATE broker_accounts SET cash = ?, buying_power = ?, equity = ? WHERE id = ?",
                     (str(cash), str(buying_power), str(equity), account_id))


def get_account(conn, account_id: str) -> dict | None:
    """One broker account, or None.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.

    Used by: brokers.routes, automations.runner.Trading, orders.routes, performance.routes, portfolios.routes.
    """
    r = conn.execute("SELECT * FROM broker_accounts WHERE id = ?", (account_id,)).fetchone()
    return account_json(r) if r else None


def list_accounts(conn) -> list[dict]:
    """Every broker account, by name.

    Args:
        conn: app DB connection.

    Used by: brokers.routes.list_accounts, automations.scheduler.sync_brokers.
    """
    return [account_json(r) for r in conn.execute("SELECT * FROM broker_accounts ORDER BY name")]


def create_account(conn, data: dict, status: str, error: str | None) -> dict:
    """Store a new broker account.

    Args:
        conn: app DB connection.
        data: name, provider, environment, credentialsRef, baseCurrency (optional).
        status: initial status (CONNECTED, ERROR or DISABLED).
        error: connection error, if any.

    Returns:
        The stored account.

    Used by: brokers.routes.create_account.
    """
    aid, ts = new_id("bka"), now_iso()
    with conn:
        conn.execute(
            "INSERT INTO broker_accounts(id, name, provider, environment, credentials_ref, base_currency, status, "
            "last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, data["name"], data["provider"], data["environment"], data["credentialsRef"],
             data.get("baseCurrency", "USD"), status, error, ts, ts),
        )
    return get_account(conn, aid)


def set_status(conn, account_id: str, status: str, error: str | None = None, synced: bool = False) -> None:
    """Record the connection status.

    Args:
        conn: app DB connection.
        account_id: the broker account's id.
        status: CONNECTED, ERROR or DISABLED.
        error: the error (None clears it).
        synced: also set last_sync_at to now.

    Used by: brokers.routes.check, orders.service.run_account_sync.
    """
    ts = now_iso()
    with conn:
        conn.execute(
            "UPDATE broker_accounts SET status = ?, last_error = ?, updated_at = ?, "
            "last_sync_at = CASE WHEN ? THEN ? ELSE last_sync_at END WHERE id = ?",
            (status, error, ts, synced, ts, account_id),
        )


def delete_account(conn, account_id: str) -> None:
    """Delete a broker account (the routes check no portfolio uses it).

    Args:
        conn: app DB connection.
        account_id: the broker account's id.

    Used by: brokers.routes.delete_account.
    """
    with conn:
        conn.execute("DELETE FROM broker_accounts WHERE id = ?", (account_id,))
