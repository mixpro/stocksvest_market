"""Higher-level market helpers built on the MarketAPI functions: a cached universe and per-symbol lookups."""

import threading
import time

from .client import MarketAPI

# Above this many symbols, ask for all symbols and filter locally (keeps URLs short).
MAX_SYMBOLS_PER_CALL = 200
UNIVERSE_TTL_SECONDS = 600
CALENDAR_TTL_SECONDS = 3600


class MarketData:
    def __init__(self, api: MarketAPI):
        self.api = api
        self._universe: tuple[float, dict[str, dict]] | None = None
        self._lock = threading.Lock()
        self._calendar: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    def universe(self) -> dict[str, dict]:
        """All halal symbols by ticker, cached for a few minutes."""
        with self._lock:
            if self._universe and time.monotonic() - self._universe[0] < UNIVERSE_TTL_SECONDS:
                return self._universe[1]
            symbols = {s["ticker"]: s for s in self.api.symbols(limit=50000)["symbols"]}
            self._universe = (time.monotonic(), symbols)
            return symbols

    @staticmethod
    def _symbols_arg(tickers: set[str]) -> list[str] | None:
        return sorted(tickers) if len(tickers) <= MAX_SYMBOLS_PER_CALL else None  # None = all symbols

    def snapshots(self, tickers: set[str]) -> dict[str, dict]:
        if not tickers:
            return {}
        data = self.api.snapshots(self._symbols_arg(tickers))["snapshots"]
        return {t: v for t, v in data.items() if t in tickers}

    def price_changes(self, tickers: set[str], amount: int, unit: str) -> dict[str, dict]:
        if not tickers:
            return {}
        data = self.api.price_change(amount, unit, self._symbols_arg(tickers))["changes"]
        return {t: v for t, v in data.items() if t in tickers}

    def calendar_days(self, start, end) -> list[dict]:
        """Trading days between two dates (inclusive), cached for an hour."""
        key = (str(start), str(end))
        with self._lock:
            hit = self._calendar.get(key)
            if hit and time.monotonic() - hit[0] < CALENDAR_TTL_SECONDS:
                return hit[1]
        days = self.api.calendar(str(start), str(end))["days"]
        with self._lock:
            self._calendar[key] = (time.monotonic(), days)
        return days

    def trading_day(self, day: str) -> dict | None:
        days = self.api.calendar(day, day)["days"]
        return days[0] if days else None

    def sector_options(self) -> list[dict]:
        """Sectors that have symbols, as {value, label} options."""
        return [{"value": s["key"], "label": s["name"]} for s in self.api.sectors()["sectors"] if s["symbols"]]
