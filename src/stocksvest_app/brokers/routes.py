"""/v1/broker-accounts"""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..deps import AppContext
from .base import BrokerError
from .registry import PROVIDERS
from . import store


class BrokerAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["ALPACA", "IBKR"]
    environment: Literal["PAPER", "LIVE"] = "PAPER"
    credentialsRef: str = Field(default="ALPACA", pattern=r"^[A-Z][A-Z0-9_]*$")

    @field_validator("environment")
    @classmethod
    def paper_only(cls, v):
        if v != "PAPER":
            raise ValueError("live trading is not enabled; only PAPER accounts can be added")
        return v

    @field_validator("provider")
    @classmethod
    def available(cls, v):
        if v not in PROVIDERS:
            raise ValueError(f"{v} is not available yet")
        return v


def snapshot_json(s) -> dict:
    return {"cash": float(s.cash), "buyingPower": float(s.buying_power), "equity": float(s.equity),
            "currency": s.currency, "at": s.at.isoformat()}


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(prefix="/v1/broker-accounts", tags=["brokers"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, account_id: str) -> dict:
        a = store.get_account(conn, account_id)
        if not a:
            raise HTTPException(404, f"broker account '{account_id}' not found")
        return a

    def check(conn, account: dict) -> dict:
        """Connect, record the status, and return the account with its capabilities and balances."""
        try:
            broker = ctx.broker_factory(account)
            snap = broker.account_snapshot()
        except BrokerError as e:
            store.set_status(conn, account["id"], "ERROR", str(e))
            return {**store.get_account(conn, account["id"]), "capabilities": None, "snapshot": None}
        store.set_status(conn, account["id"], "CONNECTED")
        return {**store.get_account(conn, account["id"]), "capabilities": broker.capabilities().to_json(),
                "snapshot": snapshot_json(snap)}

    @r.get("")
    def list_accounts(conn: Conn):
        return {"accounts": store.list_accounts(conn)}

    @r.post("", status_code=201)
    def create_account(body: BrokerAccountIn, conn: Conn):
        """Add a paper account. The connection is tested right away (status CONNECTED or ERROR)."""
        account = store.create_account(conn, body.model_dump(), "DISABLED", None)
        return check(conn, account)

    @r.get("/{account_id}")
    def get_account(account_id: str, conn: Conn):
        """The account with live balances from the broker and what it supports."""
        return check(conn, load(conn, account_id))

    @r.delete("/{account_id}", status_code=204)
    def delete_account(account_id: str, conn: Conn):
        load(conn, account_id)
        used = conn.execute("SELECT name FROM portfolios WHERE broker_account_id = ?", (account_id,)).fetchall()
        if used:
            raise HTTPException(409, f"used by portfolio: {', '.join(u['name'] for u in used)}")
        store.delete_account(conn, account_id)

    return r
