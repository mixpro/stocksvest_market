"""History endpoints: portfolio snapshots, budget performance, broker account equity."""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

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
    """The history routes (/v1/portfolios/{id}/history and /snapshot, /v1/budgets/{id}/performance, /v1/broker-accounts/{id}/equity-history).

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

    @r.get("/v1/budgets/{budget_id}/performance", tags=["history"])
    def budget_performance(budget_id: str, conn: Conn, start: str | None = None, end: str | None = None):
        """A budget's value: one point per trading day (after the close), plus `current` right now.
        returnPercent = (equity - allocated) / allocated.
        \f
        Args:
            budget_id: the budget's id (path).
            conn: app DB connection (injected).
            start: from this date (query, optional).
            end: up to this date (query, optional).
        """
        row = conn.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        if not row and not store.budget_points(conn, budget_id, None, None):
            raise HTTPException(404, f"budget '{budget_id}' not found")
        now = datetime.now(timezone.utc)
        current = None
        if row:  # a deleted budget keeps its history
            from .snapshots import ET
            current = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       **_json(snapshots.budget_values(ctx, conn, row, now.astimezone(ET).date().isoformat()))}
        return {"points": store.budget_points(conn, budget_id, start, end), "current": current}

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
