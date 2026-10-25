"""/v1/market/*: market endpoints for app clients, each backed by one MarketAPI function (all reads, except
live/watch, which asks the market service to stream real-time bars for some symbols).

Clients only need an app key; the app calls the market API with its own key.
"""

from collections.abc import Callable
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..deps import AppContext
from .client import MarketError

# Accepted values, mirroring the market API's enums (see stocksvest_market.api). Used by: the routes below.
Timeframe = Literal["1Min", "1Hour", "1Day", "1Week", "1Month", "1Year"]
Session = Literal["all", "regular"]
PeriodUnit = Literal["MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"]


class LiveWatchIn(BaseModel):
    """Body of PUT /v1/market/live/watch. Used by: router.live_watch."""
    symbols: list[str] = Field(max_length=500)
    ttlMinutes: int = Field(default=10, ge=1, le=120)


def _split(value: str | None) -> list[str] | None:
    """"A,B, C" -> ["A", "B", "C"]; empty -> None.

    Args:
        value: comma-separated query value.

    Used by: symbols, multi_bars, snapshots, price_change, volume.
    """
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def call(fn: Callable[[], dict]) -> dict:
    """Run a market call and turn failures into HTTP errors: MarketError keeps its status, anything unexpected
    becomes 502.

    Args:
        fn: the call.

    Used by: every route below.
    """
    try:
        return fn()
    except MarketError as e:
        raise HTTPException(e.status, e.detail)
    except Exception as e:  # an unexpected answer from the market API must not become a 500
        raise HTTPException(502, f"unexpected response from the market API: {type(e).__name__}")


def router(ctx: AppContext) -> APIRouter:
    """The /v1/market routes, each backed by one MarketAPI function.

    Args:
        ctx: app context (market_api and the key check).

    Used by: api.create_app.
    """
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
        """Halal symbols (market `GET /v1/symbols`).
        \f
        Args:
            search: ticker prefix or name part.
            exchange: exchange name.
            asset_type: COMMON_STOCK or ADR.
            sector: comma-separated sector keys.
            limit: page size.
            offset: rows to skip.
        """
        return call(lambda: api.symbols(search, exchange, asset_type, _split(sector), limit, offset))

    @r.get("/symbols/{symbol}")
    def symbol(symbol: str):
        """One symbol by ticker or id (market `GET /v1/symbols/{symbol}`).
        \f
        Args:
            symbol: ticker or id.
        """
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
        """Chart bars for one symbol (market `GET /v1/bars/{symbol}`).
        \f
        Args:
            symbol: ticker or id.
            timeframe: bar size.
            start: first bar time.
            end: last bar time.
            limit: most recent bars.
            session: all or regular.
        """
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
        """Bars for several comma-separated symbols (market `GET /v1/bars`).
        \f
        Args:
            symbols: comma-separated tickers or ids.
            timeframe: bar size.
            start: first bar time.
            end: last bar time.
            limit: most recent bars per symbol.
            session: all or regular.
        """
        return call(lambda: api.multi_bars(_split(symbols), timeframe, start, end, limit, session))

    @r.get("/snapshots")
    def snapshots(symbols: str | None = None):
        """Latest price and daily change (market `GET /v1/snapshots`).
        \f
        Args:
            symbols: comma-separated tickers or ids; empty = all.
        """
        return call(lambda: api.snapshots(_split(symbols)))

    @r.get("/price-change")
    def price_change(
        period_amount: Annotated[int, Query(alias="periodAmount", ge=1)],
        period_unit: Annotated[PeriodUnit, Query(alias="periodUnit")],
        symbols: str | None = None,
    ):
        """Price change over a period (market `GET /v1/price-change`).
        \f
        Args:
            period_amount: how many units back (periodAmount).
            period_unit: unit (periodUnit).
            symbols: comma-separated tickers or ids; empty = all.
        """
        return call(lambda: api.price_change(period_amount, period_unit, _split(symbols)))

    @r.get("/volume")
    def volume(days: Annotated[int, Query(ge=1, le=260)] = 20, symbols: str | None = None):
        """Average daily trading over the last completed trading days (market `GET /v1/volume`).
        \f
        Args:
            days: trading days averaged.
            symbols: comma-separated tickers or ids; empty = all.
        """
        return call(lambda: api.volume(days, _split(symbols)))

    @r.get("/indicators")
    def indicators(symbols: str | None = None):
        """Volatility, ATR, 50/200-day averages and 52-week high from completed daily bars (market
        `GET /v1/indicators`).
        \f
        Args:
            symbols: comma-separated tickers or ids; empty = all.
        """
        return call(lambda: api.indicators(_split(symbols)))

    @r.get("/returns")
    def returns(days: Annotated[int, Query(ge=2, le=260)] = 120, symbols: str | None = None):
        """Daily returns over the last completed trading days, for correlations (market `GET /v1/returns`).
        \f
        Args:
            days: completed trading days.
            symbols: comma-separated tickers or ids; empty = all.
        """
        return call(lambda: api.returns(days, _split(symbols)))

    @r.get("/quotes")
    def quotes(symbols: str):
        """Prices right now: IBKR live bid/ask/last when available, else the delayed price (market `GET /v1/quotes`).
        \f
        Args:
            symbols: comma-separated tickers or ids, at most 100.
        """
        return call(lambda: api.quotes(_split(symbols)))

    @r.get("/live")
    def live():
        """Whether real-time IBKR bars are running for the watched symbols (market `GET /v1/live`)."""
        return call(lambda: api.live_status())

    @r.put("/live/watch")
    def live_watch(body: LiveWatchIn):
        """Stream real-time IBKR bars for these symbols for a while (market `PUT /v1/live/watch`). The app's scheduler
        already sends every held symbol each minute.
        \f
        Args:
            body: LiveWatchIn.
        """
        return call(lambda: api.live_watch(body.symbols, body.ttlMinutes))

    @r.get("/calendar")
    def calendar(start: date | None = None, end: date | None = None):
        """Trading days and session times (market `GET /v1/calendar`).
        \f
        Args:
            start: first date.
            end: last date.
        """
        return call(lambda: api.calendar(start.isoformat() if start else None, end.isoformat() if end else None))

    @r.get("/status")
    def status():
        """Market sync status (market `GET /v1/status`)."""
        return call(api.status)

    return r
