"""/v1/broker-accounts"""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..deps import AppContext
from .base import BrokerError
from .registry import PROVIDERS, live_allowed
from . import store


class BrokerAccountIn(BaseModel):
    """Body of POST /v1/broker-accounts.

    Attributes:
        name: display name.
        provider: ALPACA (IBKR later; refused until available).
        environment: PAPER, or LIVE when ALLOW_LIVE=1.
        credentialsRef: names the .env variables <REF>_KEY and <REF>_SECRET.

    Used by: router.create_account.
    """
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["ALPACA", "IBKR"]
    environment: Literal["PAPER", "LIVE"] = "PAPER"
    credentialsRef: str = Field(default="ALPACA", pattern=r"^[A-Z][A-Z0-9_]*$")

    @field_validator("environment")
    @classmethod
    def paper_only(cls, v):
        """Refuse LIVE accounts unless registry.live_allowed().

        Args:
            v: the environment.
        """
        if v != "PAPER" and not live_allowed():
            raise ValueError("live trading is not enabled (ALLOW_LIVE=1); only PAPER accounts can be added")
        return v

    @field_validator("provider")
    @classmethod
    def available(cls, v):
        """Refuse providers not in registry.PROVIDERS.

        Args:
            v: the provider.
        """
        if v not in PROVIDERS:
            raise ValueError(f"{v} is not available yet")
        return v


def snapshot_json(s) -> dict:
    """An AccountSnapshot in API form.

    Args:
        s: the snapshot.

    Used by: router.check.
    """
    return {"cash": float(s.cash), "buyingPower": float(s.buying_power), "equity": float(s.equity),
            "currency": s.currency, "at": s.at.isoformat()}


def router(ctx: AppContext) -> APIRouter:
    """The /v1/broker-accounts routes.

    Args:
        ctx: app context (DB, broker factory, key check).

    Used by: api.create_app.
    """
    r = APIRouter(prefix="/v1/broker-accounts", tags=["brokers"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, account_id: str) -> dict:
        """A stored broker account, or 404.

        Args:
            conn: app DB connection.
            account_id: the account's id.
        """
        a = store.get_account(conn, account_id)
        if not a:
            raise HTTPException(404, f"broker account '{account_id}' not found")
        return a

    def overview(conn, account: dict) -> dict:
        """Portfolios on the account and what is not in any of them (cash and shares).

        Args:
            conn: app DB connection.
            account: the account dict.

        Returns:
            {portfolios, assignedCash, unassignedCash, unassignedShares, lastSync}.
        """
        from ..orders import store as orders_store
        from ..portfolios import store as portfolios_store

        portfolios = portfolios_store.list_portfolios(conn, account["id"])
        assigned = sum(p["cash"] for p in portfolios)
        last = orders_store.last_sync(conn, account["id"])
        return {"portfolios": [{"id": p["id"], "name": p["name"], "cash": p["cash"]} for p in portfolios],
                "assignedCash": assigned,
                "unassignedCash": (account["cash"] - assigned) if account["cash"] is not None else None,
                "unassignedShares": (last or {}).get("unassigned", {}).get("shares", {}), "lastSync": last}

    def check(conn, account: dict) -> dict:
        """Connect, record the status and balances, and return the account with its capabilities.

        Args:
            conn: app DB connection.
            account: the account dict.

        Returns:
            The account + overview + capabilities and snapshot (both None when the broker can't be reached).
        """
        try:
            broker = ctx.broker_factory(account)
            snap = broker.account_snapshot()
        except BrokerError as e:
            store.set_status(conn, account["id"], "ERROR", str(e))
            a = store.get_account(conn, account["id"])
            return {**a, **overview(conn, a), "capabilities": None, "snapshot": None}
        store.set_status(conn, account["id"], "CONNECTED")
        store.set_balances(conn, account["id"], snap.cash, snap.buying_power, snap.equity)
        a = store.get_account(conn, account["id"])
        return {**a, **overview(conn, a), "capabilities": broker.capabilities().to_json(),
                "snapshot": snapshot_json(snap)}

    @r.get("")
    def list_accounts(conn: Conn):
        """Every broker account (stored balances; no broker call).
        \f
        Args:
            conn: app DB connection (injected).
        """
        return {"accounts": store.list_accounts(conn)}

    @r.post("", status_code=201)
    def create_account(body: BrokerAccountIn, conn: Conn):
        """Add a paper account. The connection is tested right away (status CONNECTED or ERROR).
        \f
        Args:
            body: the account.
            conn: app DB connection (injected).
        """
        account = store.create_account(conn, body.model_dump(), "DISABLED", None)
        return check(conn, account)

    @r.get("/{account_id}")
    def get_account(account_id: str, conn: Conn):
        """The account with live balances, what it supports, its portfolios and its unassigned cash and shares.
        \f
        Args:
            account_id: the broker account's id (path).
            conn: app DB connection (injected).
        """
        return check(conn, load(conn, account_id))

    @r.delete("/{account_id}", status_code=204)
    def delete_account(account_id: str, conn: Conn):
        """Remove the account. Refused (409) while a portfolio uses it.
        \f
        Args:
            account_id: the broker account's id (path).
            conn: app DB connection (injected).
        """
        load(conn, account_id)
        used = conn.execute("SELECT name FROM portfolios WHERE broker_account_id = ?", (account_id,)).fetchall()
        if used:
            raise HTTPException(409, f"used by portfolio: {', '.join(u['name'] for u in used)}")
        store.delete_account(conn, account_id)

    return r
