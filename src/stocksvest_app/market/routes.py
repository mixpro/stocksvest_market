"""/v1/market/*: read-only market endpoints for app clients, each backed by one MarketAPI function.

Clients only need an app key; the app calls the market API with its own key.
"""

from collections.abc import Callable
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import AppContext
from .client import MarketError

Timeframe = Literal["1Min", "1Hour", "1Day", "1Week", "1Month", "1Year"]
Session = Literal["all", "regular"]
PeriodUnit = Literal["MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"]


def _split(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def call(fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except MarketError as e:
        raise HTTPException(e.status, e.detail)
    except Exception as e:  # an unexpected answer from the market API must not become a 500
        raise HTTPException(502, f"unexpected response from the market API: {type(e).__name__}")


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(prefix="/v1/market", tags=["market"], dependencies=[Depends(ctx.require_key)])
    api = ctx.market_api

    @r.get("/symbols")
    def symbols(
        search: str | None = None,
        exchange: str | None = None,
        asset_type: Annotated[str | None, Query(alias="assetType")] = None,
        sector: Annotated[str | None, Query(description="Comma-separated sector keys")] = None,
        limit: Annotated[int | None, Query(ge=1, le=50000)] = None,
        offset: Annotated[int | None, Query(ge=0)] = None,
    ):
        """Halal symbols (market `GET /v1/symbols`)."""
        return call(lambda: api.symbols(search, exchange, asset_type, _split(sector), limit, offset))

    @r.get("/symbols/{symbol}")
    def symbol(symbol: str):
        """One symbol by ticker or id (market `GET /v1/symbols/{symbol}`)."""
        return call(lambda: api.symbol(symbol))

    @r.get("/sectors")
    def sectors():
        """Sector keys with symbol counts (market `GET /v1/sectors`)."""
        return call(api.sectors)

    @r.get("/bars/{symbol}")
    def bars(
        symbol: str,
        timeframe: Timeframe | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=50000)] = None,
        session: Session | None = None,
    ):
        """Chart bars for one symbol (market `GET /v1/bars/{symbol}`)."""
        return call(lambda: api.bars(symbol, timeframe, start, end, limit, session))

    @r.get("/bars")
    def multi_bars(
        symbols: str,
        timeframe: Timeframe | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=50000)] = None,
        session: Session | None = None,
    ):
        """Bars for several comma-separated symbols (market `GET /v1/bars`)."""
        return call(lambda: api.multi_bars(_split(symbols), timeframe, start, end, limit, session))

    @r.get("/snapshots")
    def snapshots(symbols: str | None = None):
        """Latest price and daily change (market `GET /v1/snapshots`)."""
        return call(lambda: api.snapshots(_split(symbols)))

    @r.get("/price-change")
    def price_change(
        period_amount: Annotated[int, Query(alias="periodAmount", ge=1)],
        period_unit: Annotated[PeriodUnit, Query(alias="periodUnit")],
        symbols: str | None = None,
    ):
        """Price change over a period (market `GET /v1/price-change`)."""
        return call(lambda: api.price_change(period_amount, period_unit, _split(symbols)))

    @r.get("/calendar")
    def calendar(start: date | None = None, end: date | None = None):
        """Trading days and session times (market `GET /v1/calendar`)."""
        return call(lambda: api.calendar(start.isoformat() if start else None, end.isoformat() if end else None))

    @r.get("/status")
    def status():
        """Market sync status (market `GET /v1/status`)."""
        return call(api.status)

    return r
