"""Higher-level market helpers built on the MarketAPI functions: a cached universe and per-symbol lookups."""

import threading
import time

from .client import MarketAPI

# Above this many symbols, ask for all symbols and filter locally (keeps URLs short).
MAX_SYMBOLS_PER_CALL = 200
UNIVERSE_TTL_SECONDS = 600
CALENDAR_TTL_SECONDS = 3600


class MarketData:
    """Helpers on top of MarketAPI that the engine and jobs need: a cached universe, batched per-symbol lookups,
    and a cached trading calendar.

    Used by: deps.AppContext.market; filters.engine.Evaluator, automations (runner, scheduler, routes),
    performance.snapshots, portfolios.routes.
    """

    def __init__(self, api: MarketAPI):
        """Args:
            api: the market endpoint functions.
        """
        self.api = api
        self._universe: tuple[float, dict[str, dict]] | None = None
        self._lock = threading.Lock()
        self._calendar: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    def universe(self) -> dict[str, dict]:
        """All halal symbols by ticker, cached for UNIVERSE_TTL_SECONDS.

        Used by: filters.engine (UNIVERSE input, SYMBOL_TYPE and SECTOR actions, result rows), portfolios.routes
        (halal check before a manual order).
        """
        with self._lock:
            if self._universe and time.monotonic() - self._universe[0] < UNIVERSE_TTL_SECONDS:
                return self._universe[1]
            symbols = {s["ticker"]: s for s in self.api.symbols(limit=50000)["symbols"]}
            self._universe = (time.monotonic(), symbols)
            return symbols

    @staticmethod
    def _symbols_arg(tickers: set[str]) -> list[str] | None:
        """The `symbols` argument for a lookup: the sorted tickers, or None (= all symbols) above
        MAX_SYMBOLS_PER_CALL so URLs stay short.

        Args:
            tickers: the tickers needed.
        """
        return sorted(tickers) if len(tickers) <= MAX_SYMBOLS_PER_CALL else None  # None = all symbols

    def snapshots(self, tickers: set[str]) -> dict[str, dict]:
        """Market snapshots (latest price, daily change, ...) for these tickers only. Tickers outside the halal
        universe (e.g. a holding that is no longer halal) are not asked for: the market answers 404 for them.

        Args:
            tickers: tickers wanted; empty = no call.

        Used by: filters.engine (SYMBOL_PRICE, result rows), automations planner context, portfolios.routes
        (position values), performance.snapshots.
        """
        tickers = tickers & self.universe().keys() if tickers else tickers
        if not tickers:
            return {}
        data = self.api.snapshots(self._symbols_arg(tickers))["snapshots"]
        return {t: v for t, v in data.items() if t in tickers}

    def price_changes(self, tickers: set[str], amount: int, unit: str) -> dict[str, dict]:
        """Price changes over a period for these tickers only (halal universe only, as in snapshots).

        Args:
            tickers: tickers wanted; empty = no call.
            amount: how many units back.
            unit: "MINUTES" ... "YEARS".

        Used by: filters.engine (PRICE_CHANGE action).
        """
        tickers = tickers & self.universe().keys() if tickers else tickers
        if not tickers:
            return {}
        data = self.api.price_change(amount, unit, self._symbols_arg(tickers))["changes"]
        return {t: v for t, v in data.items() if t in tickers}

    def volumes(self, tickers: set[str], days: int) -> dict[str, dict]:
        """Average daily trading over the last `days` completed trading days for these tickers only (halal
        universe only, as in snapshots).

        Args:
            tickers: tickers wanted; empty = no call.
            days: trading days averaged.

        Returns:
            ticker -> {averageVolume, averageDollarVolume, daysTraded} (None values without enough history).

        Used by: filters.engine (AVERAGE_VOLUME action).
        """
        tickers = tickers & self.universe().keys() if tickers else tickers
        if not tickers:
            return {}
        data = self.api.volume(days, self._symbols_arg(tickers))["volumes"]
        return {t: v for t, v in data.items() if t in tickers}

    def indicators(self, tickers: set[str]) -> dict[str, dict]:
        """Technical indicators (market GET /v1/indicators) for these tickers only (halal universe only, as in
        snapshots).

        Args:
            tickers: tickers wanted; empty = no call.

        Returns:
            ticker -> {close, volatility20, atr14Percent, sma50, sma200, priceVsSma50Percent, priceVsSma200Percent,
            high52w, fromHigh52wPercent}.

        Used by: filters.engine (INDICATOR action), automations.planner.add_rank_data.
        """
        tickers = tickers & self.universe().keys() if tickers else tickers
        if not tickers:
            return {}
        data = self.api.indicators(self._symbols_arg(tickers))["indicators"]
        return {t: v for t, v in data.items() if t in tickers}

    def live_quotes(self, tickers: set[str]) -> dict[str, dict]:
        """Prices right now (IBKR live when the market service can get them, else its delayed price), for the few
        symbols about to be traded. Not cached: every call asks again.

        Args:
            tickers: tickers wanted (halal universe only, like snapshots); empty = no call.

        Returns:
            ticker -> {source (IBKR | DELAYED), bid, ask, last, price, time, ageSeconds}.

        Raises:
            MarketError: the market service failed.

        Used by: automations.runner._place_orders, portfolios.routes (manual orders, position prices).
        """
        tickers = sorted(tickers & self.universe().keys()) if tickers else []
        out: dict[str, dict] = {}
        for i in range(0, len(tickers), 100):  # the market service takes at most 100 per call
            out.update(self.api.quotes(tickers[i:i + 100])["quotes"])
        return out

    def calendar_days(self, start, end) -> list[dict]:
        """Trading days between two dates (inclusive), cached for CALENDAR_TTL_SECONDS.

        Args:
            start: date or "YYYY-MM-DD".
            end: date or "YYYY-MM-DD".

        Used by: automations.timing (MARKET schedules, via the scheduler and next-run endpoints).
        """
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
        """One day's session times, or None when the market is closed.

        Args:
            day: "YYYY-MM-DD" (New York).

        Used by: filters.engine (LAST_CLOSE), automations.runner.market_context, performance.snapshots.scheduled.
        """
        days = self.api.calendar(day, day)["days"]
        return days[0] if days else None

    def sector_options(self) -> list[dict]:
        """Sectors that have symbols, as {value, label} options for the filter editor.

        Used by: filters.routes.filter_actions.
        """
        return [{"value": s["key"], "label": s["name"]} for s in self.api.sectors()["sectors"] if s["symbols"]]
