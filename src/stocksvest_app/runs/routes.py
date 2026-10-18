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
    """Run `fn` and store the outcome (also when it fails) in the run history.

    Args:
        ctx: app context (retention setting).
        conn: app DB connection.
        kind: "FILTER" or "FILTER_GROUP".
        subject_id: the filter or group id.
        definition: its configuration at run time.
        fn: the run; returns the result dict, or raises HTTPException / MarketError.

    Returns:
        The stored run.

    Raises:
        HTTPException, MarketError: re-raised after the failed run is stored.

    Used by: filters.routes.run_filter, filters.routes.run_group.
    """
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
    """The /v1/runs routes.

    Args:
        ctx: app context (DB connection and key check).

    Used by: api.create_app.
    """
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
        """History of all app runs (kept RUN_RETENTION_DAYS, default ~6 months). Newest first.
        \f
        Args:
            conn: app DB connection (injected).
            kind: FILTER, FILTER_GROUP or AUTOMATION.
            subject_id: one filter/group/automation (subjectId).
            since: ISO time.
            limit: maximum runs.
            include_result: add definitions and results (includeResult).
        """
        return {"runs": store.list_runs(conn, kind, subject_id, since, limit, include_result)}

    @r.get("/{run_id}")
    def get_run(run_id: str, conn: Conn):
        """One run with its definition and result.
        \f
        Args:
            run_id: the run's id.
            conn: app DB connection (injected).
        """
        run = store.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, f"run '{run_id}' not found")
        return run

    return r
