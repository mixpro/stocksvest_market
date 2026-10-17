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
        if symbol not in UNIVERSE:
            raise MarketError(404, f"unknown symbol '{symbol}'")
        return UNIVERSE[symbol]

    def sectors(self):
        return {"sectors": [{"key": "TECHNOLOGY", "name": "Technology", "symbols": 2},
                            {"key": "ENERGY", "name": "Energy", "symbols": 0}], "unassigned": 0}

    def bars(self, symbol, timeframe=None, start=None, end=None, limit=None, session=None):
        self.calls.append(("bars", symbol, timeframe, start, end, limit, session))
        return {"symbol": symbol, "timeframe": timeframe, "bars": []}

    def multi_bars(self, symbols, timeframe=None, start=None, end=None, limit=None, session=None):
        self.calls.append(("multi_bars", symbols, timeframe, start, end, limit, session))
        return {"timeframe": timeframe, "bars": {s: [] for s in symbols}}

    def snapshots(self, symbols=None):
        self.calls.append(("snapshots", symbols))
        return {"snapshots": {t: SNAPS[t] for t in (symbols or SNAPS)}}

    def price_change(self, period_amount, period_unit, symbols=None):
        self.calls.append(("price_change", period_amount, period_unit, symbols))
        return {"periodAmount": period_amount, "periodUnit": period_unit,
                "changes": {t: CHANGES[t] for t in (symbols or CHANGES)}}

    def calendar(self, start=None, end=None):
        self.calls.append(("calendar", start, end))
        d, last = date.fromisoformat(start), date.fromisoformat(end or start)
        days = []
        while d <= last:
            if d.weekday() < 5 and d not in HOLIDAYS:
                days.append(trading_day(d, self.regular_close))
            d += timedelta(days=1)
        return {"days": days}

    def status(self):
        return {"symbols": len(UNIVERSE), "lastIncrementalAt": self.last_sync, "dataDelayMinutes": 16}
