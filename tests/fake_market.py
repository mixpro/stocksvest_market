"""In-memory MarketAPI for app tests: same functions as MarketClient, canned data."""

from stocksvest_app.market.client import MarketError

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
    def __init__(self, fail: bool = False, regular_close: str = "2026-10-16T16:00:00-04:00"):
        self.fail = fail
        self.regular_close = regular_close
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
        return {"days": [{"date": start, "regularClose": self.regular_close}]}

    def status(self):
        return {"symbols": len(UNIVERSE), "lastIncrementalAt": None, "dataDelayMinutes": 16}
