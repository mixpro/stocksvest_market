"""Live quotes: the market service's /v1/quotes endpoint and the IBKR connection (with a fake IB client)."""

import math
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import FakeAlpaca
from stocksvest_market.api import create_app
from stocksvest_market.ibkr import IbkrQuotes, ib_symbol
from test_api import READ, seeded  # noqa: F401  (fixture)


class FakeSource:
    connected = True
    last_error = None
    last_quote_at = None

    def __init__(self, quotes):
        self._quotes = quotes
        self.asked = []

    def quotes(self, tickers):
        self.asked.append(list(tickers))
        return {t: q for t, q in self._quotes.items() if t in tickers}


def client_with(settings, source):
    return TestClient(create_app(settings, client_factory=lambda s: FakeAlpaca(), quote_source=source))


def test_quotes_live_and_delayed_fallback(settings, seeded):  # noqa: F811
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source = FakeSource({"S0001": {"bid": 28.0, "ask": 28.01, "last": 12.6, "time": now, "marketDataType": 1}})
    with client_with(settings, source) as c:
        body = c.get("/v1/quotes?symbols=S0001,S0002", headers=READ).json()
    live, delayed = body["quotes"]["S0001"], body["quotes"]["S0002"]
    assert live["source"] == "IBKR" and live["price"] == 28.005 and live["ageSeconds"] <= 2  # rounded, no float noise
    assert (live["bid"], live["ask"], live["last"]) == (28.0, 28.01, 12.6)
    assert delayed["source"] == "DELAYED" and delayed["price"] is None  # S0002 has no bars in the fixture
    assert source.asked == [["S0001", "S0002"]] and body["liveQuotes"]["connected"] is True


def test_quotes_without_ibkr_use_the_delayed_price(settings, seeded):  # noqa: F811
    with client_with(settings, None) as c:
        body = c.get("/v1/quotes?symbols=S0001", headers=READ).json()
        status = c.get("/v1/status", headers=READ).json()["liveQuotes"]
    q = body["quotes"]["S0001"]
    assert q["source"] == "DELAYED" and q["price"] == 13 and q["time"] == "2026-10-13T14:00:00Z"
    assert status == {"enabled": False, "connected": False, "lastError": None, "lastQuoteAt": None}


def test_quotes_validation(settings, seeded):  # noqa: F811
    with client_with(settings, None) as c:
        assert c.get("/v1/quotes", headers=READ).status_code == 422
        assert c.get("/v1/quotes?symbols=NOPE", headers=READ).status_code == 404
        assert c.get("/v1/quotes?symbols=S0001").status_code == 401


# ------------------------------------------------------------------ the IBKR connection, with a fake IB client


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


NAN = float("nan")


class FakeIB:
    """Just enough of ib_async.IB: connect, qualify, stream tickers, cancel."""

    book = {  # symbol -> (bid, ask, last, marketDataType)
        "AAPL": (340.35, 340.39, 340.41, 1),
        "BRK B": (500.0, 500.2, 500.1, 1),
        "SLOW": (NAN, NAN, NAN, 1),        # never quotes
        "DELAY": (10.0, 10.1, 10.05, 3),   # IBKR's delayed data: not a live quote
    }
    fail_connect = 0

    def __init__(self):
        self.errorEvent = Event()
        self.connected = False
        self.live = {}
        self.cancelled = []
        self.connect_args = None

    def isConnected(self):
        return self.connected

    async def connectAsync(self, host, port, clientId, readonly, timeout):
        if FakeIB.fail_connect:
            FakeIB.fail_connect -= 1
            raise ConnectionRefusedError("gateway down")
        self.connect_args = (host, port, clientId, readonly)
        self.connected = True

    def reqMarketDataType(self, t):
        self.market_data_type = t

    async def qualifyContractsAsync(self, c):
        if c.symbol not in self.book:
            return []
        c.conId = abs(hash(c.symbol)) % 10_000 + 1
        return [c]

    def reqMktData(self, c, generic, snapshot, regulatory):
        assert snapshot is False and regulatory is False  # streaming only: no paid snapshots
        bid, ask, last, mdt = self.book[c.symbol]
        t = SimpleNamespace(bid=bid, ask=ask, last=last, marketDataType=mdt,
                            time=datetime.now(timezone.utc) if not math.isnan(last) else None)
        self.live[c.symbol] = t
        return t

    def cancelMktData(self, c):
        self.cancelled.append(c.symbol)

    history = {}  # symbol -> 1-minute bars returned by reqHistoricalDataAsync

    async def reqHistoricalDataAsync(self, c, end, duration, size, what, useRTH, timeout):
        assert (duration, size, what, useRTH) == ("1800 S", "1 min", "TRADES", False)
        return FakeIB.history.get(c.symbol, [])

    def reqRealTimeBars(self, c, size, what, useRTH):
        assert (size, what, useRTH) == (5, "TRADES", False)
        bars = RealTimeBars(c.symbol)
        self.streams = getattr(self, "streams", {})
        self.streams[c.symbol] = bars
        return bars

    def cancelRealTimeBars(self, bars):
        self.cancelled_bars = getattr(self, "cancelled_bars", []) + [bars.symbol]

    def disconnect(self):
        self.connected = False


class RealTimeBars(list):
    """ib_async's RealTimeBarList: a list of bars with an updateEvent(bars, hasNewBar)."""

    def __init__(self, symbol):
        super().__init__()
        self.symbol = symbol
        self.updateEvent = Event()

    def push(self, t, o, h, low, c, v):
        self.append(SimpleNamespace(time=t, open_=o, high=h, low=low, close=c, volume=v))
        self.updateEvent.emit(self, True)


def started(settings, factory=FakeIB):
    from dataclasses import replace
    q = IbkrQuotes(replace(settings, ibkr_client_id=77), ib_factory=factory)
    q.start()
    for _ in range(100):
        if q.connected:
            return q
        time.sleep(0.05)
    raise AssertionError("never connected")


def test_ibkr_quotes(settings):
    q = started(settings)
    try:
        assert q._ib.connect_args == ("127.0.0.1", 4002, 77, True) and q._ib.market_data_type == 1
        out = q.quotes(["AAPL", "BRK.B", "SLOW", "DELAY", "UNKNOWN", "AAPL"], timeout=0.5)
        assert set(out) == {"AAPL", "BRK.B"}  # no quote in time, delayed data and unknown symbols are left out
        assert (out["AAPL"]["bid"], out["AAPL"]["ask"], out["AAPL"]["last"]) == (340.35, 340.39, 340.41)
        assert sorted(q._ib.cancelled) == ["AAPL", "BRK B", "DELAY", "SLOW"]  # every subscription is closed
        assert q.last_quote_at is not None
        q._ib.errorEvent.emit(5, 10197, "No market data during competing live session", None)
        q._ib.errorEvent.emit(5, 2104, "Market data farm connection is OK", None)
        assert q.last_error.startswith("10197")
    finally:
        q.stop()


def test_ibkr_reconnects_after_the_gateway_comes_back(settings, monkeypatch):
    monkeypatch.setattr("stocksvest_market.ibkr.asyncio.sleep", _fast_sleep)
    FakeIB.fail_connect = 2
    q = started(settings)
    try:
        assert q.connected and q.last_error is None
        q._ib.connected = False  # the gateway's daily restart
        for _ in range(100):
            if q.connected and q._ib.isConnected():
                break
            time.sleep(0.05)
        assert q.quotes(["AAPL"], timeout=0.5)["AAPL"]["last"] == 340.41
    finally:
        FakeIB.fail_connect = 0
        q.stop()


_real_sleep = __import__("asyncio").sleep


async def _fast_sleep(seconds):
    await _real_sleep(min(seconds, 0.05))


def test_quotes_when_not_connected(settings):
    q = IbkrQuotes(settings, ib_factory=FakeIB)
    assert q.quotes(["AAPL"]) == {}


def test_ib_symbol():
    assert ib_symbol("BRK.B") == "BRK B" and ib_symbol("AAPL") == "AAPL"


# ------------------------------------------------------------------ live bars


def wait_for(cond, tries=100):
    for _ in range(tries):
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("condition never met")


def live_rows(settings):
    from stocksvest_market import db
    conn = db.connect(settings.db_path)
    try:
        return [tuple(r) for r in conn.execute("SELECT symbol_id, ts, o, h, l, c, v FROM bars_live_1min ORDER BY ts")]
    finally:
        conn.close()


def test_live_bars_backfill_stream_and_expiry(settings, seeded, monkeypatch):  # noqa: F811
    from dataclasses import replace
    monkeypatch.setattr("stocksvest_market.ibkr.asyncio.sleep", _fast_sleep)
    monkeypatch.setitem(FakeIB.book, "S0001", (12.0, 12.1, 12.05, 1))
    monkeypatch.setitem(FakeIB.book, "S0002", (5.0, 5.1, 5.05, 1))
    t0 = datetime(2026, 10, 26, 14, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(FakeIB, "history", {"S0001": [SimpleNamespace(date=t0, open=12.0, high=12.2, low=11.9,
                                                                       close=12.1, volume=300)]})
    q = started(replace(settings, ibkr_live_bars_max=1))
    try:
        out = q.watch(["S0001", "S0002"], ttl_seconds=60)
        assert len(out["watching"]) == 1 and len(out["overLimit"]) == 1  # the limit holds
        q.watch(["S0001"], ttl_seconds=60)  # renewed last: S0001 wins the only slot
        wait_for(lambda: "S0001" in getattr(q._ib, "streams", {}))
        wait_for(lambda: live_rows(settings))
        assert live_rows(settings) == [("id-S0001", int(t0.timestamp()), 12.0, 12.2, 11.9, 12.1, 300.0)]  # backfill
        bars = q._ib.streams["S0001"]
        t1 = datetime(2026, 10, 26, 14, 31, tzinfo=timezone.utc)
        bars.push(t1, 12.1, 12.3, 12.1, 12.25, 100)
        bars.push(t1.replace(second=5), 12.25, 12.4, 12.0, 12.05, 50)  # same minute: rolled up
        wait_for(lambda: len(live_rows(settings)) == 2)
        assert live_rows(settings)[1] == ("id-S0001", int(t1.timestamp()), 12.1, 12.4, 12.0, 12.05, 150.0)
        assert q.live_status()["streaming"] == 1 and q.live_status()["lastBarAt"] is not None
        q._watch["S0001"] = 0  # the watch ran out: its stream stops and S0002 gets the free slot
        wait_for(lambda: "S0001" in getattr(q._ib, "cancelled_bars", []))
        wait_for(lambda: "S0002" in q._ib.streams)
        assert q.live_status()["watching"] == 1 and q.live_status()["overLimit"] == 0
    finally:
        q.stop()


class WatchingSource(FakeSource):
    def __init__(self):
        super().__init__({})
        self.watched = []

    def watch(self, tickers, ttl_seconds):
        self.watched.append((list(tickers), ttl_seconds))
        return {"watching": sorted(tickers), "overLimit": []}

    def live_status(self):
        return {"watching": 1, "streaming": 1, "overLimit": 0, "lastBarAt": "2026-10-26T14:31:05+00:00"}


def test_live_minutes_make_prices_current(settings, seeded):  # noqa: F811
    from test_api import ts
    source = WatchingSource()
    with client_with(settings, source) as c:
        r = c.put("/v1/live/watch", headers=READ, json={"symbols": ["s0001", "NOPE"], "ttlMinutes": 5}).json()
        assert r["watching"] == ["S0001"] and r["unknown"] == ["NOPE"] and source.watched == [(["S0001"], 300)]
        assert c.get("/v1/live", headers=READ).json()["streaming"] == 1
        assert c.get("/v1/status", headers=READ).json()["liveBars"]["enabled"] is True
        # The newest SIP minute is Oct 13 10:00 NY (13); live minutes from IBKR are newer.
        seeded.executemany("INSERT INTO bars_live_1min VALUES ('id-S0001', ?, ?, ?, ?, ?, 10, NULL, NULL)",
                           [(ts(2026, 10, 13, 14, 15), 14, 14, 14, 14), (ts(2026, 10, 13, 14, 20), 12, 12, 12, 12)])
        seeded.commit()
        snap = c.get("/v1/snapshots?symbols=S0001", headers=READ).json()["snapshots"]["S0001"]
        assert snap["latestPrice"] == 12 and snap["latestTime"] == "2026-10-13T14:20:00Z"
        change = c.get("/v1/price-change?symbols=S0001&periodAmount=3&periodUnit=MINUTES", headers=READ).json()
        assert change["changes"]["S0001"]["fromPrice"] == 14 and change["changes"]["S0001"]["toPrice"] == 12
        assert c.put("/v1/live/watch", json={"symbols": []}).status_code == 401


def test_live_watch_without_ibkr(settings, seeded):  # noqa: F811
    with client_with(settings, None) as c:
        r = c.put("/v1/live/watch", headers=READ, json={"symbols": ["S0001"]}).json()
    assert r["watching"] == [] and r["liveBars"]["enabled"] is False
