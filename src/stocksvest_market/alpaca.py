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
    """Alpaca refused a request (non-retryable status), or kept failing after retries, or keys are missing.

    Used by: AlpacaClient (raised); callers let it propagate to the sync job, which records it as the
    scheduler's last error.
    """

    pass


class AlpacaClient:
    """Minimal Alpaca REST client for what the market service needs: tradable assets, the trading calendar and
    historical bars. Calls are rate-limited across threads and retried on network errors, 429 and 5xx.

    Used by: sync (all sync functions), scheduler.Scheduler, api.create_app (admin endpoints), __main__.main.
    """

    def __init__(self, settings: Settings):
        """Create a client.

        Args:
            settings: provides the keys, base URLs, feed and requests_per_minute.

        Raises:
            AlpacaError: ALPACA_KEY or ALPACA_SECRET is empty.
        """
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
        """GET a URL with rate limiting and retries (up to 6 attempts, exponential backoff or Retry-After).

        Args:
            url: full URL.
            params: query parameters.

        Returns:
            The decoded JSON body.

        Raises:
            AlpacaError: a non-retryable status (e.g. 401, 403, 422), or still failing after all attempts.
        """
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
        """All active US equity assets (GET /v2/assets). Each has id, symbol, exchange, tradable, status, ...

        Used by: sync.sync_symbols (to know which listed symbols Alpaca can trade, and their stable ids).
        """
        return self._get(
            f"{self.settings.alpaca_trading_url}/v2/assets",
            {"status": "active", "asset_class": "us_equity"},
        )

    def calendar(self, start: date, end: date) -> list[dict]:
        """Trading days between two dates (GET /v2/calendar). Each has date, open, close, session_open,
        session_close (New York times, "HH:MM" or "HHMM").

        Args:
            start: first date (inclusive).
            end: last date (inclusive).

        Used by: sync.sync_calendar.
        """
        return self._get(
            f"{self.settings.alpaca_trading_url}/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )

    def bars(
        self, symbols: list[str], timeframe: str, start: datetime, end: datetime
    ) -> Iterator[tuple[str, dict]]:
        """Yield (ticker, bar) for split- and dividend-adjusted bars, following pagination.

        Args:
            symbols: tickers in Alpaca format (up to ~100 per call keeps URLs reasonable).
            timeframe: "1Min", "1Hour" or "1Day".
            start: first bar start (inclusive), timezone-aware.
            end: last bar start (inclusive), timezone-aware; must be at least SIP_DELAY_MINUTES in the past on
                the free plan.

        Yields:
            (ticker, bar) where bar has t (ISO time), o, h, l, c, v, n (trade count), vw (VWAP).

        Used by: sync._fetch_and_store, sync.find_readjusted.
        """
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


def _alpaca_time(t: str) -> datetime:
    """An Alpaca timestamp (nanoseconds, "Z") as an aware datetime. Used by: AlpacaQuotes.quotes."""
    head, _, frac = t.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00")


class AlpacaQuotes:
    """Live quotes from Alpaca's real-time SIP snapshots (paid data plan): latest bid/ask and last trade.
    Same interface as ibkr.IbkrQuotes' quotes, so api.quotes uses whichever is configured.

    Attributes:
        source: "ALPACA", the quotes' `source` in GET /v1/quotes.
        connected: the last request worked.
        last_error / last_quote_at: shown by GET /v1/status.

    Used by: api.create_app (when IBKR is off and Alpaca keys are set).
    """

    source = "ALPACA"

    def __init__(self, settings: Settings, client: AlpacaClient | None = None):
        self.settings = settings
        self.client = client or AlpacaClient(settings)
        self.connected = True
        self.last_error: str | None = None
        self.last_quote_at: datetime | None = None

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        """ticker -> {bid, ask, last, time (ISO)} from one snapshots request; {} when Alpaca fails. A one-sided,
        crossed or empty quote keeps only the last trade (bid and ask None).

        Args:
            tickers: the market service's tickers (Alpaca's own symbols).
        """
        if not tickers:
            return {}
        try:
            data = self.client._get(f"{self.settings.alpaca_data_url}/v2/stocks/snapshots",
                                    {"symbols": ",".join(tickers), "feed": self.settings.alpaca_feed})
        except (AlpacaError, requests.RequestException) as e:
            self.connected, self.last_error = False, f"snapshots: {e}"
            log.warning("Alpaca quotes failed: %s", e)
            return {}
        self.connected, self.last_error = True, None
        out = {}
        for t, snap in (data or {}).items():
            q, tr = (snap or {}).get("latestQuote") or {}, (snap or {}).get("latestTrade") or {}
            bid, ask = q.get("bp") or None, q.get("ap") or None
            if bid is None or ask is None or ask < bid:
                bid = ask = None
            stamp = q.get("t") if bid is not None else tr.get("t")
            if not stamp or (bid is None and tr.get("p") is None):
                continue
            at = _alpaca_time(stamp)
            out[t] = {"bid": bid, "ask": ask, "last": tr.get("p"), "time": at.isoformat()}
            self.last_quote_at = max(self.last_quote_at or at, at)
        return out
