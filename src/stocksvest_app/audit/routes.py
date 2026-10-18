"""/v1/activity: read the activity log."""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import AppContext
from . import store


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(prefix="/v1/activity", tags=["activity"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    @r.get("")
    def activity(
        conn: Conn,
        entity_type: Annotated[str | None, Query(alias="entityType")] = None,
        entity_id: Annotated[str | None, Query(alias="entityId", description="Entries about it or mentioning it")] = None,
        related_id: Annotated[str | None, Query(alias="relatedId")] = None,
        action: str | None = None,
        source: Literal["API", "SCHEDULER", "SYNC", "SYSTEM"] | None = None,
        outcome: Literal["OK", "FAILED"] | None = None,
        correlation_id: Annotated[str | None, Query(alias="correlationId")] = None,
        since: str | None = None,
        until: str | None = None,
        before_id: Annotated[int | None, Query(alias="beforeId", description="Paging: entries older than this id")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ):
        """Newest first. Page back with beforeId = the last id you received."""
        entries = store.query(conn, entity_type=entity_type, entity_id=entity_id, related_id=related_id, action=action,
                              source=source, outcome=outcome, correlation=correlation_id, since=since, until=until,
                              before_id=before_id, limit=limit)
        return {"entries": entries, "nextBeforeId": entries[-1]["id"] if len(entries) == limit else None}

    @r.get("/actions")
    def actions(conn: Conn):
        """Every action name recorded so far, with counts."""
        rows = conn.execute("SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY action")
        return {"actions": [{"action": a["action"], "count": a["n"]} for a in rows]}

    @r.get("/{entry_id}")
    def entry(entry_id: int, conn: Conn):
        row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"activity entry {entry_id} not found")
        return store.entry_json(row)

    return r
