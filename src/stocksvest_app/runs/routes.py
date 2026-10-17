"""/v1/runs: the history of all app runs, for analysis."""

import sqlite3
import time
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import AppContext
from ..market.client import MarketError
from . import store


def record(ctx: AppContext, conn: sqlite3.Connection, kind: str, subject_id: str, definition: dict,
           fn: Callable[[], dict]) -> dict:
    """Run `fn` and store the outcome (also when it fails) in the run history."""
    started = time.monotonic()
    elapsed = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
    try:
        result = fn()
    except (HTTPException, MarketError) as e:
        store.add_run(conn, kind, subject_id, definition, elapsed(), error=str(e.detail),
                      retention_days=ctx.settings.run_retention_days)
        raise
    return store.add_run(conn, kind, subject_id, definition, elapsed(), result=result,
                         retention_days=ctx.settings.run_retention_days)


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(prefix="/v1/runs", tags=["runs"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    @r.get("")
    def list_runs(
        conn: Conn,
        kind: str | None = None,
        subject_id: Annotated[str | None, Query(alias="subjectId")] = None,
        since: str | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 100,
        include_result: Annotated[bool, Query(alias="includeResult")] = False,
    ):
        """History of all app runs (kept RUN_RETENTION_DAYS, default ~6 months). Newest first."""
        return {"runs": store.list_runs(conn, kind, subject_id, since, limit, include_result)}

    @r.get("/{run_id}")
    def get_run(run_id: str, conn: Conn):
        run = store.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, f"run '{run_id}' not found")
        return run

    return r
