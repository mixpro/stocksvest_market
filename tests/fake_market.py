"""In-memory MarketAPI for app tests: same functions as MarketClient, canned data."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from stocksvest_app.market.client import MarketError

ET = ZoneInfo("America/New_York")
HOLIDAYS = {date(2026, 11, 26)}  # Thanksgiving
EARLY_CLOSES = {date(2026, 11, 27)}  # closes 13:00, after-hours until 17:00


def trading_day(d: date, regular_close: str | None = None) -> dict:
    at = lambda h, m=0: datetime.combine(d, time(h, m), ET).isoformat()  # noqa: E731
    early = d in EARLY_CLOSES
    close = at(13) if early else at(16)
    if regular_close and regular_close.startswith(d.isoformat()):
        close = regular_close
    return {"date": d.isoformat(), "preMarketOpen": at(4), "regularOpen": at(9, 30), "regularClose": close,
            "afterHoursClose": at(17) if early else at(20), "isEarlyClose": early}

UNIVERSE = {
    "AAA": {"id": "id-AAA", "ticker": "AAA", "name": "Aaa Inc", "assetType": "COMMON_STOCK", "sector": "TECHNOLOGY"},
    "BBB": {"id": "id-BBB", "ticker": "BBB", "name": "Bbb Inc", "assetType": "COMMON_STOCK", "sector": "HEALTHCARE"},
    "CCC": {"id": "id-CCC", "ticker": "CCC", "name": "Ccc ADR", "assetType": "ADR", "sector": "TECHNOLOGY"},
    "DDD": {"id": "id-DDD", "ticker": "DDD", "name": "Ddd Inc", "assetType": "COMMON_STOCK", "sector": None},
}
SNAPS = {
    "AAA": {"latestPrice": 10.0, "latestTime": "t", "dailyClose": 10.0, "dailyDate": "2026-10-16",
            "previousClose": 9.0, "dailyChangePercent": 11.1},
    "BBB": {"latestPrice": 50.0, "latestTime": "t", "dailyClose": 50.0, "dailyDate": "2026-10-16",
            "previousClose": 60.0, "dailyChangePercent": -16.7},
    "CCC": {"latestPrice": 5.0, "latestTime": "t", "dailyClose": 5.0, "dailyDate": "2026-10-16",
            "previousClose": 5.0, "dailyChangePercent": 0.0},
    "DDD": {"latestPrice": None, "latestTime": None, "dailyClose": None, "dailyDate": None,
            "previousClose": None, "dailyChangePercent": None},
}
CHANGES = {"AAA": {"change": 2.0, "changePercent": 25.0}, "BBB": {"change": -5.0, "changePercent": -9.0},
           "CCC": {"change": 0.1, "changePercent": 2.0}, "DDD": {"change": None, "changePercent": None}}
# Average daily trading (20 days); CCC barely trades, DDD has no history.
VOLUMES = {"AAA": {"averageVolume": 500_000.0, "averageDollarVolume": 5_000_000.0, "daysTraded": 20},
           "BBB": {"averageVolume": 40_000.0, "averageDollarVolume": 2_000_000.0, "daysTraded": 20},
           "CCC": {"averageVolume": 300.0, "averageDollarVolume": 1_500.0, "daysTraded": 6},
           "DDD": {"averageVolume": None, "averageDollarVolume": None, "daysTraded": None}}


class FakeMarket:
    def __init__(self, fail: bool = False, regular_close: str | None = None, last_sync: str | None = None):
        self.fail = fail
        self.regular_close = regular_close
        self.last_sync = last_sync
        self.calls: list[tuple] = []

    def _check(self):
        if self.fail:
            raise MarketError(502, "market API unreachable")

    def symbols(self, search=None, exchange=None, asset_type=None, sector=None, limit=None, offset=None):
        self._check()
        self.calls.append(("symbols", search, exchange, asset_type, sector, limit, offset))
        return {"total": len(UNIVERSE), "symbols": list(UNIVERSE.values())}

    def symbol(self, symbol):
        self._check()
        if symbol not in UNIVERSE:
            raise MarketError(404, f"unknown symbol '{symbol}'")
        return UNIVERSE[symbol]

    def sectors(self):
        self._check()
        return {"sectors": [{"key": "TECHNOLOGY", "name": "Technology", "symbols": 2},
                            {"key": "ENERGY", "name": "Energy", "symbols": 0}], "unassigned": 0}

    def bars(self, symbol, timeframe=None, start=None, end=None, limit=None, session=None):
        self._check()
        self.calls.append(("bars", symbol, timeframe, start, end, limit, session))
        return {"symbol": symbol, "timeframe": timeframe, "bars": []}

    def multi_bars(self, symbols, timeframe=None, start=None, end=None, limit=None, session=None):
        self._check()
        self.calls.append(("multi_bars", symbols, timeframe, start, end, limit, session))
        return {"timeframe": timeframe, "bars": {s: [] for s in symbols}}

    def snapshots(self, symbols=None):
        self._check()
        self.calls.append(("snapshots", symbols))
        return {"snapshots": {t: SNAPS[t] for t in (symbols or SNAPS)}}

    def quotes(self, symbols):
        """Live quotes when set in self.live (ticker -> quote), else the delayed snapshot price."""
        self._check()
        self.calls.append(("quotes", symbols))
        live = getattr(self, "live", {})
        out = {}
        for t in symbols:
            if t in live:
                out[t] = {"source": getattr(self, "live_source", "IBKR"), "ageSeconds": 1, "time": "t", **live[t]}
            else:
                p = SNAPS[t]["latestPrice"]
                out[t] = {"source": "DELAYED", "bid": None, "ask": None, "last": p, "price": p, "time": "t",
                          "ageSeconds": 960}
        return {"quotes": out, "liveQuotes": {"enabled": bool(live), "connected": bool(live)}}

    def price_change(self, period_amount, period_unit, symbols=None):
        self._check()
        self.calls.append(("price_change", period_amount, period_unit, symbols))
        return {"periodAmount": period_amount, "periodUnit": period_unit,
                "changes": {t: CHANGES[t] for t in (symbols or CHANGES)}}

    def live_status(self):
        self._check()
        return {"enabled": True, "watching": len(getattr(self, "watched", [[]])[-1]), "streaming": 0, "overLimit": 0,
                "lastBarAt": None}

    def live_watch(self, symbols, ttl_minutes=10):
        self._check()
        self.watched = getattr(self, "watched", []) + [list(symbols)]
        self.calls.append(("live_watch", list(symbols), ttl_minutes))
        return {"watching": sorted(symbols), "overLimit": [], "unknown": [], "liveBars": self.live_status()}

    def volume(self, days, symbols=None):
        self._check()
        self.calls.append(("volume", days, symbols))
        return {"days": days, "from": "f", "to": "t", "volumes": {t: VOLUMES[t] for t in (symbols or VOLUMES)}}

    def calendar(self, start=None, end=None):
        self._check()
        self.calls.append(("calendar", start, end))
        start = start or date.today().isoformat()  # like the real market API: today + 30 days
        end = end or (date.fromisoformat(start) + timedelta(days=30)).isoformat()
        d, last = date.fromisoformat(start), date.fromisoformat(end)
        days = []
        while d <= last:
            if d.weekday() < 5 and d not in HOLIDAYS:
                days.append(trading_day(d, self.regular_close))
            d += timedelta(days=1)
        return {"days": days}

    def status(self):
        self._check()
        return {"symbols": len(UNIVERSE), "lastIncrementalAt": self.last_sync, "dataDelayMinutes": 16}
