"""History endpoints: portfolio snapshots, automation performance, broker account equity."""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..automations import store as automations_store
from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..deps import AppContext
from ..portfolios import store as portfolios_store
from . import snapshots, store

EQUITY_CACHE_SECONDS = 600  # broker equity history is cached this long. Used by: router.equity_history


def _json(values: dict) -> dict:
    """Snapshot values in API form: camelCase keys, Decimals as floats.

    Args:
        values: snake_case values.

    Used by: router (current values).
    """
    return {store._camel(k): (float(v) if v is not None and not isinstance(v, (int, str)) else v)
            for k, v in values.items()}


def router(ctx: AppContext) -> APIRouter:
    """The history routes (/v1/portfolios/{id}/history and /snapshot, /v1/automations/{id}/performance, /v1/broker-accounts/{id}/equity-history).

    Args:
        ctx: app context (DB, market helpers, broker factory, key check).

    Used by: api.create_app.
    """
    r = APIRouter(dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]
    equity_cache: dict[tuple, tuple[float, list]] = {}

    def portfolio(conn, portfolio_id: str) -> dict:
        """A stored portfolio, or 404.

        Args:
            conn: app DB connection.
            portfolio_id: the portfolio's id.
        """
        p = portfolios_store.get_portfolio(conn, portfolio_id)
        if not p:
            raise HTTPException(404, f"portfolio '{portfolio_id}' not found")
        return p

    @r.get("/v1/portfolios/{portfolio_id}/history", tags=["history"])
    def portfolio_history(portfolio_id: str, conn: Conn, start: str | None = None, end: str | None = None,
                          kind: Literal["INTERVAL", "CLOSE", "MANUAL"] | None = None):
        """Snapshots oldest first (every 15 min in the regular session, end of day, on request), plus `current`:
        the same values right now. Use kind=CLOSE for one point per day.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            start: from this time, ISO (query, optional).
            end: up to this time, ISO (query, optional).
            kind: only INTERVAL, CLOSE or MANUAL snapshots (query, optional).
        """
        p = portfolio(conn, portfolio_id)
        now = datetime.now(timezone.utc)
        return {"points": store.portfolio_points(conn, portfolio_id, start, end, kind),
                "current": {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            **_json(snapshots.portfolio_values(ctx, conn, p))}}

    @r.post("/v1/portfolios/{portfolio_id}/snapshot", tags=["history"])
    def snapshot_now(portfolio_id: str, conn: Conn):
        """Record a MANUAL snapshot now (scheduled ones happen on their own).
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
        """
        p = portfolio(conn, portfolio_id)
        now = datetime.now(timezone.utc)
        return {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "MANUAL",
                **_json(snapshots.snapshot_portfolio(ctx, conn, p, "MANUAL", now))}

    @r.get("/v1/automations/{automation_id}/performance", tags=["history"])
    def automation_performance(automation_id: str, conn: Conn, start: str | None = None, end: str | None = None):
        """One point per trading day (after the close), plus `current` right now. returnPercent =
        (equity - allocated) / allocated.
        \f
        Args:
            automation_id: the automation's id (path).
            conn: app DB connection (injected).
            start: from this date (query, optional).
            end: up to this date (query, optional).
        """
        if not automations_store.get_automation(conn, automation_id):
            raise HTTPException(404, f"automation '{automation_id}' not found")
        row = conn.execute("SELECT * FROM budget_pools WHERE automation_id = ?", (automation_id,)).fetchone()
        now = datetime.now(timezone.utc)
        current = None
        if row:
            from .snapshots import ET
            current = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       **_json(snapshots.automation_values(ctx, conn, row, now.astimezone(ET).date().isoformat()))}
        return {"points": store.automation_points(conn, automation_id, start, end), "current": current}

    @r.get("/v1/broker-accounts/{account_id}/equity-history", tags=["history"])
    def equity_history(account_id: str, conn: Conn,
                       period: Annotated[str, Query(pattern=r"^\d+[DWMA]$")] = "1M",
                       timeframe: Literal["1Min", "5Min", "15Min", "1H", "1D"] = "1D"):
        """The whole broker account's equity as reported by the broker (includes history from before the app
        recorded anything). Cached for 10 minutes.
        \f
        Args:
            account_id: the broker account's id (path).
            conn: app DB connection (injected).
            period: how far back: a number + D, W, M or A (year), e.g. 1M (query).
            timeframe: point spacing: 1Min, 5Min, 15Min, 1H or 1D (query).
        """
        account = brokers_store.get_account(conn, account_id)
        if not account:
            raise HTTPException(404, f"broker account '{account_id}' not found")
        key = (account_id, period, timeframe)
        hit = equity_cache.get(key)
        if hit and time.monotonic() - hit[0] < EQUITY_CACHE_SECONDS:
            return {"points": hit[1], "cached": True}
        try:
            broker = ctx.broker_factory(account)
            if not hasattr(broker, "equity_history"):
                raise HTTPException(501, f"{account['provider']} does not report equity history")
            points = broker.equity_history(period, timeframe)
        except BrokerError as e:
            raise HTTPException(502, str(e))
        equity_cache[key] = (time.monotonic(), points)
        return {"points": points, "cached": False}

    return r
