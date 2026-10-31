"""/v1/events and /v1/watchers: what the watchers found, and switching them on/off and tuning them."""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import AppContext
from . import store
from .thread import WATCHERS


class WatcherIn(BaseModel):
    """Body of PUT /v1/watchers/{name}: only the fields given change.

    Attributes:
        enabled: run it or not.
        settings: some of its settings, e.g. {"dropPercent": 10}; unknown keys are refused.
    """
    enabled: bool | None = None
    settings: dict[str, float] | None = None


def router(ctx: AppContext) -> APIRouter:
    """The /v1/events and /v1/watchers routes.

    Used by: api.create_app.
    """
    r = APIRouter(dependencies=[Depends(ctx.require_key)], tags=["watchers"])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def view(w, c: dict) -> dict:
        return {"name": w.NAME, "description": (w.__doc__ or "").strip().split("\n")[0], "hours": w.HOURS,
                "everySeconds": int(w.EVERY.total_seconds()), "enabled": c["enabled"], "settings": c["settings"],
                "lastRunAt": c["lastRunAt"], "lastError": c["lastError"]}

    @r.get("/v1/events")
    def get_events(conn: Conn, status: Literal["NEW", "DONE", "FAILED"] | None = None,
                   limit: Annotated[int, Query(ge=1, le=500)] = 100):
        """Events the watchers found, newest first."""
        return {"events": store.list_events(conn, status, limit)}

    @r.get("/v1/watchers")
    def get_watchers(conn: Conn):
        """Every watcher with its on/off state, settings, last run and last error."""
        return {"watchers": [view(w, store.config(conn, w)) for w in WATCHERS.values()]}

    @r.put("/v1/watchers/{name}")
    def put_watcher(name: str, body: WatcherIn, conn: Conn):
        """Switch a watcher on or off, or change some of its settings."""
        w = WATCHERS.get(name)
        if not w:
            raise HTTPException(404, f"no watcher {name!r}")
        changes = {}
        if body.enabled is not None:
            changes["enabled"] = body.enabled
        if body.settings is not None:
            unknown = set(body.settings) - set(w.DEFAULTS)
            if unknown:
                raise HTTPException(422, f"unknown settings: {sorted(unknown)}")
            if any(v <= 0 for v in body.settings.values()):
                raise HTTPException(422, "settings must be above 0")
            changes["settings"] = {**store.config(conn, w)["settings"], **body.settings}
        return view(w, store.save_config(conn, w, **changes))

    return r
