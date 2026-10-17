"""Thin Alpaca REST client: assets, trading calendar and historical bars."""

import logging
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime

import requests

from .config import Settings

log = logging.getLogger(__name__)


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    def __init__(self, settings: Settings):
        if not settings.alpaca_key or not settings.alpaca_secret:
            raise AlpacaError("ALPACA_KEY and ALPACA_SECRET must be set")
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {"APCA-API-KEY-ID": settings.alpaca_key, "APCA-API-SECRET-KEY": settings.alpaca_secret}
        )
        self._min_interval = 60.0 / settings.requests_per_minute
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _get(self, url: str, params: dict | None = None) -> dict | list:
        for attempt in range(6):
            with self._lock:
                wait = self._last_call + self._min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                log.warning("Alpaca request failed (%s), retrying", e)
                time.sleep(2**attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                delay = float(r.headers.get("Retry-After", 2**attempt))
                log.warning("Alpaca %s, retrying in %.0fs", r.status_code, delay)
                time.sleep(delay)
                continue
            if r.status_code != 200:
                raise AlpacaError(f"{r.status_code} {url}: {r.text[:300]}")
            return r.json()
        raise AlpacaError(f"giving up on {url}")

    def assets(self) -> list[dict]:
        return self._get(
            f"{self.settings.alpaca_trading_url}/v2/assets",
            {"status": "active", "asset_class": "us_equity"},
        )

    def calendar(self, start: date, end: date) -> list[dict]:
        return self._get(
            f"{self.settings.alpaca_trading_url}/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )

    def bars(
        self, symbols: list[str], timeframe: str, start: datetime, end: datetime
    ) -> Iterator[tuple[str, dict]]:
        """Yield (ticker, bar) for split- and dividend-adjusted bars, following pagination."""
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": "all",
            "feed": self.settings.alpaca_feed,
            "limit": 10000,
            "sort": "asc",
        }
        url = f"{self.settings.alpaca_data_url}/v2/stocks/bars"
        while True:
            data = self._get(url, params)
            for ticker, bars in (data.get("bars") or {}).items():
                for bar in bars:
                    yield ticker, bar
            token = data.get("next_page_token")
            if not token:
                return
            params["page_token"] = token
