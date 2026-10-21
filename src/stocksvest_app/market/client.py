"""Market API client: one function per market endpoint.

All market URLs live in ENDPOINTS. Functions return the market's JSON unchanged; higher-level helpers
(caching, batching) are in market/data.py.
"""

from typing import Protocol, runtime_checkable
from urllib.parse import quote

import requests

from ..config import Settings

# Function name -> market API path. The only place market URLs are written.
# Used by: MarketClient._get; tests check every MarketAPI function has an entry and an app route.
ENDPOINTS = {
    "symbols": "/v1/symbols",
    "symbol": "/v1/symbols/{symbol}",
    "sectors": "/v1/sectors",
    "bars": "/v1/bars/{symbol}",
    "multi_bars": "/v1/bars",
    "snapshots": "/v1/snapshots",
    "price_change": "/v1/price-change",
    "quotes": "/v1/quotes",
    "calendar": "/v1/calendar",
    "status": "/v1/status",
}


class MarketError(RuntimeError):
    """The market API failed or refused a call.

    Attributes:
        status: the market's HTTP status for 4xx answers (e.g. 404 unknown symbol), else 502 (unreachable or 5xx).
        detail: readable message, prefixed "market API".

    Used by: MarketClient (raised); market.routes.call, filters routes (_http), automations runner/routes,
    portfolios routes, performance.snapshots (caught and turned into HTTP errors or failed runs).
    """

    def __init__(self, status: int, detail: str):
        """Args:
            status: HTTP status to report.
            detail: message.
        """
        super().__init__(detail)
        self.status = status
        self.detail = detail


@runtime_checkable
class MarketAPI(Protocol):
    """The market endpoints the app uses: one function per endpoint, returning the market's JSON unchanged.
    MarketClient implements it over HTTP; tests use a fake with the same signatures (checked by a test).
    Every function raises MarketError on failure.

    Used by: deps.AppContext.market_api, market.data.MarketData, market.routes (one app route per function).
    """

    def symbols(self, search: str | None = None, exchange: str | None = None, asset_type: str | None = None,
                sector: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> dict:
        """GET /v1/symbols: {"total", "symbols": [{id, ticker, name, exchange, assetType, sector, ...}]}.

        Args:
            search: ticker prefix or part of the name.
            exchange: "NASDAQ", "NYSE", ...
            asset_type: "COMMON_STOCK" or "ADR".
            sector: sector keys (any of them).
            limit: page size.
            offset: rows to skip.
        """
        ...

    def symbol(self, symbol: str) -> dict:
        """GET /v1/symbols/{symbol}: one symbol.

        Args:
            symbol: ticker or id.
        """
        ...

    def sectors(self) -> dict:
        """GET /v1/sectors: {"sectors": [{key, name, symbols}], "unassigned"}."""
        ...

    def bars(self, symbol: str, timeframe: str | None = None, start: str | None = None, end: str | None = None,
             limit: int | None = None, session: str | None = None) -> dict:
        """GET /v1/bars/{symbol}: {"symbol", "timeframe", "bars": [{t, o, h, l, c, v, n, vw}]}.

        Args:
            symbol: ticker or id.
            timeframe: "1Min", "1Hour", "1Day", "1Week", "1Month" or "1Year".
            start: first bar time (date or datetime; New York time without a zone).
            end: last bar time.
            limit: most recent bars to return.
            session: "all" or "regular".
        """
        ...

    def multi_bars(self, symbols: list[str], timeframe: str | None = None, start: str | None = None,
                   end: str | None = None, limit: int | None = None, session: str | None = None) -> dict:
        """GET /v1/bars: {"timeframe", "bars": {ticker: [bars]}}.

        Args:
            symbols: tickers or ids.
            timeframe: as for bars.
            start: as for bars.
            end: as for bars.
            limit: most recent bars per symbol.
            session: as for bars.
        """
        ...

    def snapshots(self, symbols: list[str] | None = None) -> dict:
        """GET /v1/snapshots: {"snapshots": {ticker: {latestPrice, latestTime, dailyClose, dailyDate, dailyVolume,
        previousClose, dailyChange, dailyChangePercent}}}.

        Args:
            symbols: tickers or ids; None = every symbol.
        """
        ...

    def price_change(self, period_amount: int, period_unit: str, symbols: list[str] | None = None) -> dict:
        """GET /v1/price-change: {"changes": {ticker: {fromTime, fromPrice, toTime, toPrice, change,
        changePercent}}}.

        Args:
            period_amount: how many units back.
            period_unit: "MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS" or "YEARS".
            symbols: tickers or ids; None = every symbol.
        """
        ...

    def quotes(self, symbols: list[str]) -> dict:
        """GET /v1/quotes: {"quotes": {ticker: {source (IBKR | DELAYED), bid, ask, last, price, time, ageSeconds}},
        "liveQuotes": {enabled, connected, lastError, lastQuoteAt}}. Live from IBKR when it can answer.

        Args:
            symbols: tickers or ids (at most 100).
        """
        ...

    def calendar(self, start: str | None = None, end: str | None = None) -> dict:
        """GET /v1/calendar: {"days": [{date, preMarketOpen, regularOpen, regularClose, afterHoursClose,
        isEarlyClose}]}; trading days only.

        Args:
            start: "YYYY-MM-DD"; default today.
            end: "YYYY-MM-DD"; default start + 30 days.
        """
        ...

    def status(self) -> dict:
        """GET /v1/status: symbol and bar counts, lastIncrementalAt, dataDelayMinutes, ..."""
        ...


def _query(**params) -> dict:
    """Drop unset params and join lists with commas, as the market API expects.

    Args:
        params: query parameters by their market API names (camelCase where the API uses it).

    Used by: MarketClient's endpoint functions.
    """
    return {k: ",".join(v) if isinstance(v, list) else v for k, v in params.items() if v is not None}


class MarketClient:
    """MarketAPI over HTTP with the app's market key (MARKET_URL, MARKET_API_KEY). See MarketAPI for each function.

    Used by: api.create_app (the default market_factory).
    """

    def __init__(self, settings: Settings):
        """Args:
            settings: provides market_url and market_api_key.
        """
        self.base = settings.market_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = settings.market_api_key

    def _get(self, endpoint: str, path_params: dict | None = None, query: dict | None = None) -> dict:
        """Call one market endpoint.

        Args:
            endpoint: key of ENDPOINTS.
            path_params: values for the path's {placeholders} (URL-encoded here).
            query: query parameters (see _query).

        Returns:
            The decoded JSON body.

        Raises:
            MarketError: unreachable (502), 4xx (same status), 5xx (502).
        """
        path = ENDPOINTS[endpoint].format(**{k: quote(v, safe="") for k, v in (path_params or {}).items()})
        try:
            r = self.session.get(f"{self.base}{path}", params=query or None, timeout=60)
        except requests.RequestException as e:
            raise MarketError(502, f"market API unreachable: {e}") from e
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise MarketError(r.status_code if r.status_code < 500 else 502, f"market API: {detail}")
        return r.json()

    def symbols(self, search=None, exchange=None, asset_type=None, sector=None, limit=None, offset=None) -> dict:
        """GET /v1/symbols (see MarketAPI.symbols).

        Args:
            search: ticker prefix or name part.
            exchange: exchange name.
            asset_type: sent as assetType.
            sector: sector keys, sent comma-separated.
            limit: page size.
            offset: rows to skip.
        """
        return self._get("symbols", query=_query(search=search, exchange=exchange, assetType=asset_type,
                                                 sector=sector, limit=limit, offset=offset))

    def symbol(self, symbol) -> dict:
        """GET /v1/symbols/{symbol}.

        Args:
            symbol: ticker or id.
        """
        return self._get("symbol", {"symbol": symbol})

    def sectors(self) -> dict:
        """GET /v1/sectors."""
        return self._get("sectors")

    def bars(self, symbol, timeframe=None, start=None, end=None, limit=None, session=None) -> dict:
        """GET /v1/bars/{symbol} (see MarketAPI.bars).

        Args:
            symbol: ticker or id.
            timeframe: bar size.
            start: first bar time.
            end: last bar time.
            limit: most recent bars.
            session: "all" or "regular".
        """
        return self._get("bars", {"symbol": symbol},
                         _query(timeframe=timeframe, start=start, end=end, limit=limit, session=session))

    def multi_bars(self, symbols, timeframe=None, start=None, end=None, limit=None, session=None) -> dict:
        """GET /v1/bars (see MarketAPI.multi_bars).

        Args:
            symbols: tickers or ids, sent comma-separated.
            timeframe: bar size.
            start: first bar time.
            end: last bar time.
            limit: most recent bars per symbol.
            session: "all" or "regular".
        """
        return self._get("multi_bars", query=_query(symbols=symbols, timeframe=timeframe, start=start, end=end,
                                                    limit=limit, session=session))

    def snapshots(self, symbols=None) -> dict:
        """GET /v1/snapshots.

        Args:
            symbols: tickers or ids; None = all.
        """
        return self._get("snapshots", query=_query(symbols=symbols))

    def price_change(self, period_amount, period_unit, symbols=None) -> dict:
        """GET /v1/price-change.

        Args:
            period_amount: sent as periodAmount.
            period_unit: sent as periodUnit.
            symbols: tickers or ids; None = all.
        """
        return self._get("price_change", query=_query(periodAmount=period_amount, periodUnit=period_unit,
                                                      symbols=symbols))

    def quotes(self, symbols) -> dict:
        """GET /v1/quotes.

        Args:
            symbols: tickers or ids (at most 100).
        """
        return self._get("quotes", query=_query(symbols=symbols))

    def calendar(self, start=None, end=None) -> dict:
        """GET /v1/calendar.

        Args:
            start: "YYYY-MM-DD".
            end: "YYYY-MM-DD".
        """
        return self._get("calendar", query=_query(start=start, end=end))

    def status(self) -> dict:
        """GET /v1/status."""
        return self._get("status")
