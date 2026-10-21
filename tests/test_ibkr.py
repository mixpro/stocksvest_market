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

    def disconnect(self):
        self.connected = False


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
