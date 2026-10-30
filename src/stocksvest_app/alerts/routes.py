"""/v1/push: the phone app's push token and which notifications it gets."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import AppContext
from . import push
from .checks import _put


class DeviceIn(BaseModel):
    """Body of POST /v1/push/devices.

    Attributes:
        token: the Expo push token, "ExponentPushToken[...]".
    """
    token: str = Field(pattern=r"^Expo(nent)?PushToken\[.+\]$", max_length=200)


def router(ctx: AppContext) -> APIRouter:
    """The /v1/push routes.

    Used by: api.create_app.
    """
    r = APIRouter(dependencies=[Depends(ctx.require_key)], tags=["push"])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def state(conn) -> dict:
        off = push.disabled(conn)
        return {"devices": len(push.tokens(conn)), "alertsEnabled": ctx.settings.alerts_enabled,
                "categories": [{"key": k, "label": v, "enabled": k not in off} for k, v in push.CATEGORIES.items()]}

    @r.get("/v1/push")
    def get_push(conn: Conn):
        """How many phones are registered, and each notification category with its on/off state."""
        return state(conn)

    @r.post("/v1/push/devices")
    def add_device(body: DeviceIn, conn: Conn):
        """Register a phone (the app calls this on every start; registering again is fine)."""
        _put(conn, "push:devices", {"tokens": [*[t for t in push.tokens(conn) if t != body.token], body.token]})
        return state(conn)

    @r.delete("/v1/push/devices/{token}")
    def remove_device(token: str, conn: Conn):
        """Stop pushing to a phone."""
        if token not in push.tokens(conn):
            raise HTTPException(404, "this phone isn't registered")
        _put(conn, "push:devices", {"tokens": [t for t in push.tokens(conn) if t != token]})
        return state(conn)

    @r.put("/v1/push/categories")
    def set_categories(body: dict[str, bool], conn: Conn):
        """Switch categories on or off: {"fills": false, ...}. Categories not named keep their state."""
        unknown = set(body) - set(push.CATEGORIES)
        if unknown:
            raise HTTPException(422, f"unknown categories: {sorted(unknown)}")
        off = {k for k in push.disabled(conn) if k not in body} | {k for k, on in body.items() if not on}
        _put(conn, "push:off", {"categories": sorted(off)})
        return state(conn)

    @r.post("/v1/push/test")
    def test(conn: Conn):
        """Send a test notification to every registered phone."""
        return {"sent": push.send(conn, "test", "StocksVest", "Notifications work.")}

    return r
