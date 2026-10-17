"""Market API client: one function per market endpoint.

All market URLs live in ENDPOINTS. Functions return the market's JSON unchanged; higher-level helpers
(caching, batching) are in market/data.py.
"""

from typing import Protocol, runtime_checkable
from urllib.parse import quote

import requests

from ..config import Settings

# Function name -> market API path. The only place market URLs are written.
ENDPOINTS = {
    "symbols": "/v1/symbols",
    "symbol": "/v1/symbols/{symbol}",
    "sectors": "/v1/sectors",
    "bars": "/v1/bars/{symbol}",
    "multi_bars": "/v1/bars",
    "snapshots": "/v1/snapshots",
    "price_change": "/v1/price-change",
    "calendar": "/v1/calendar",
    "status": "/v1/status",
}


class MarketError(RuntimeError):
    """`status` is the market's HTTP status, or 502 when the market API could not be reached."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@runtime_checkable
class MarketAPI(Protocol):
    """The market endpoints the app uses. MarketClient implements it over HTTP; tests use a fake."""

    def symbols(self, search: str | None = None, exchange: str | None = None, asset_type: str | None = None,
                sector: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> dict: ...

    def symbol(self, symbol: str) -> dict: ...

    def sectors(self) -> dict: ...

    def bars(self, symbol: str, timeframe: str | None = None, start: str | None = None, end: str | None = None,
             limit: int | None = None, session: str | None = None) -> dict: ...

    def multi_bars(self, symbols: list[str], timeframe: str | None = None, start: str | None = None,
                   end: str | None = None, limit: int | None = None, session: str | None = None) -> dict: ...

    def snapshots(self, symbols: list[str] | None = None) -> dict: ...

    def price_change(self, period_amount: int, period_unit: str, symbols: list[str] | None = None) -> dict: ...

    def calendar(self, start: str | None = None, end: str | None = None) -> dict: ...

    def status(self) -> dict: ...


def _query(**params) -> dict:
    """Drop unset params and join lists with commas, as the market API expects."""
    return {k: ",".join(v) if isinstance(v, list) else v for k, v in params.items() if v is not None}


class MarketClient:
    def __init__(self, settings: Settings):
        self.base = settings.market_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = settings.market_api_key

    def _get(self, endpoint: str, path_params: dict | None = None, query: dict | None = None) -> dict:
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
        return self._get("symbols", query=_query(search=search, exchange=exchange, assetType=asset_type,
                                                 sector=sector, limit=limit, offset=offset))

    def symbol(self, symbol) -> dict:
        return self._get("symbol", {"symbol": symbol})

    def sectors(self) -> dict:
        return self._get("sectors")

    def bars(self, symbol, timeframe=None, start=None, end=None, limit=None, session=None) -> dict:
        return self._get("bars", {"symbol": symbol},
                         _query(timeframe=timeframe, start=start, end=end, limit=limit, session=session))

    def multi_bars(self, symbols, timeframe=None, start=None, end=None, limit=None, session=None) -> dict:
        return self._get("multi_bars", query=_query(symbols=symbols, timeframe=timeframe, start=start, end=end,
                                                    limit=limit, session=session))

    def snapshots(self, symbols=None) -> dict:
        return self._get("snapshots", query=_query(symbols=symbols))

    def price_change(self, period_amount, period_unit, symbols=None) -> dict:
        return self._get("price_change", query=_query(periodAmount=period_amount, periodUnit=period_unit,
                                                      symbols=symbols))

    def calendar(self, start=None, end=None) -> dict:
        return self._get("calendar", query=_query(start=start, end=end))

    def status(self) -> dict:
        return self._get("status")
