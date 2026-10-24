import inspect

import pytest
import requests
from fastapi.testclient import TestClient

from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings
from stocksvest_app.market.client import ENDPOINTS, MarketAPI, MarketClient, MarketError

KEY = {"X-API-Key": "app-key"}
API_FUNCTIONS = [n for n, f in inspect.getmembers(MarketAPI, inspect.isfunction) if not n.startswith("_")]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    monkeypatch.setenv("MARKET_URL", "http://market:8000")
    monkeypatch.setenv("MARKET_API_KEY", "market-key")
    return Settings()


class Recorder:
    """Stands in for requests.Session: records each GET and returns a canned response."""

    def __init__(self, status=200, body=None, error=None):
        self.headers, self.requests = {}, []
        self.status, self.body, self.error = status, body if body is not None else {"ok": True}, error

    def get(self, url, params=None, timeout=None):
        if self.error:
            raise self.error
        self.requests.append((url, params))
        resp = requests.Response()
        resp.status_code = self.status
        resp._content = __import__("json").dumps(self.body).encode()
        return resp

    def put(self, url, json=None, timeout=None):
        self.puts = getattr(self, "puts", []) + [(url, json)]
        return self.get(url)


def client_with(settings, **kw) -> tuple[MarketClient, Recorder]:
    c = MarketClient(settings)
    rec = Recorder(**kw)
    rec.headers.update(c.session.headers)
    c.session = rec
    return c, rec


def test_every_endpoint_has_a_function_and_the_fake_matches():
    assert set(API_FUNCTIONS) == set(ENDPOINTS)
    for name in API_FUNCTIONS:
        expected = list(inspect.signature(getattr(MarketAPI, name)).parameters)
        assert list(inspect.signature(getattr(MarketClient, name)).parameters) == expected, name
        assert list(inspect.signature(getattr(FakeMarket, name)).parameters) == expected, name
    assert isinstance(FakeMarket(), MarketAPI)


@pytest.mark.parametrize("call, path, params", [
    (lambda m: m.symbols(limit=50000), "/v1/symbols", {"limit": 50000}),
    (lambda m: m.symbols("AA", "NYSE", "ADR", ["TECHNOLOGY", "ENERGY"], 10, 20), "/v1/symbols",
     {"search": "AA", "exchange": "NYSE", "assetType": "ADR", "sector": "TECHNOLOGY,ENERGY", "limit": 10, "offset": 20}),
    (lambda m: m.symbol("BRK.B"), "/v1/symbols/BRK.B", None),
    (lambda m: m.sectors(), "/v1/sectors", None),
    (lambda m: m.bars("AAPL", "1Hour", "2026-10-01", None, 100, "regular"), "/v1/bars/AAPL",
     {"timeframe": "1Hour", "start": "2026-10-01", "limit": 100, "session": "regular"}),
    (lambda m: m.multi_bars(["AAPL", "MSFT"], "1Day"), "/v1/bars", {"symbols": "AAPL,MSFT", "timeframe": "1Day"}),
    (lambda m: m.snapshots(), "/v1/snapshots", None),
    (lambda m: m.snapshots(["AAPL"]), "/v1/snapshots", {"symbols": "AAPL"}),
    (lambda m: m.price_change(5, "DAYS", ["AAPL", "MSFT"]), "/v1/price-change",
     {"periodAmount": 5, "periodUnit": "DAYS", "symbols": "AAPL,MSFT"}),
    (lambda m: m.quotes(["AAPL", "JRSH"]), "/v1/quotes", {"symbols": "AAPL,JRSH"}),
    (lambda m: m.live_status(), "/v1/live", None),
    (lambda m: m.calendar("2026-10-16", "2026-10-16"), "/v1/calendar", {"start": "2026-10-16", "end": "2026-10-16"}),
    (lambda m: m.status(), "/v1/status", None),
])
def test_client_function_maps_to_market_url(settings, call, path, params):
    client, rec = client_with(settings)
    assert call(client) == {"ok": True}
    assert rec.requests == [(f"http://market:8000{path}", params)]
    assert rec.headers["X-API-Key"] == "market-key"


def test_live_watch_is_a_put_with_a_body(settings):
    client, rec = client_with(settings)
    client.live_watch(["AAA", "BBB"], 10)
    assert rec.puts == [("http://market:8000/v1/live/watch", {"symbols": ["AAA", "BBB"], "ttlMinutes": 10})]


def test_client_errors(settings):
    c, _ = client_with(settings, status=404, body={"detail": "unknown symbol 'ZZZ'"})
    with pytest.raises(MarketError) as e:
        c.symbol("ZZZ")
    assert e.value.status == 404 and "unknown symbol" in e.value.detail
    c, _ = client_with(settings, status=500, body={"detail": "boom"})
    with pytest.raises(MarketError) as e:
        c.status()
    assert e.value.status == 502
    c, _ = client_with(settings, error=requests.ConnectionError("refused"))
    with pytest.raises(MarketError) as e:
        c.status()
    assert e.value.status == 502 and "unreachable" in e.value.detail


def test_market_routes_call_the_matching_function(settings):
    fake = FakeMarket()
    with TestClient(create_app(settings, market_factory=lambda s: fake)) as c:
        assert c.get("/v1/market/symbols").status_code == 401
        r = c.get("/v1/market/bars/AAA?timeframe=1Hour&limit=5&session=regular", headers=KEY)
        assert r.status_code == 200
        assert fake.calls[-1] == ("bars", "AAA", "1Hour", None, None, 5, "regular")
        c.get("/v1/market/bars?symbols=AAA,BBB&timeframe=1Day", headers=KEY)
        assert fake.calls[-1] == ("multi_bars", ["AAA", "BBB"], "1Day", None, None, None, None)
        c.get("/v1/market/symbols?sector=TECHNOLOGY,ENERGY&assetType=ADR", headers=KEY)
        assert fake.calls[-1] == ("symbols", None, None, "ADR", ["TECHNOLOGY", "ENERGY"], None, None)
        c.get("/v1/market/price-change?periodAmount=2&periodUnit=WEEKS&symbols=AAA", headers=KEY)
        assert fake.calls[-1] == ("price_change", 2, "WEEKS", ["AAA"])
        q = c.get("/v1/market/quotes?symbols=AAA,BBB", headers=KEY).json()["quotes"]
        assert fake.calls[-1] == ("quotes", ["AAA", "BBB"]) and q["AAA"]["source"] == "DELAYED"
        assert c.get("/v1/market/symbols/ZZZ", headers=KEY).status_code == 404  # market status forwarded
        assert c.get("/v1/market/bars/AAA?timeframe=2Days", headers=KEY).status_code == 422  # validated by the app
        assert c.get("/v1/market/status", headers=KEY).json()["dataDelayMinutes"] == 16
        assert c.get("/v1/market/admin/halal", headers=KEY).status_code == 404  # no generic pass-through
        r = c.put("/v1/market/live/watch", headers=KEY, json={"symbols": ["AAA"], "ttlMinutes": 3})
        assert r.status_code == 200 and fake.calls[-1] == ("live_watch", ["AAA"], 3)
        assert c.get("/v1/market/live", headers=KEY).json()["enabled"] is True


def test_every_market_function_has_an_app_route(settings):
    app = create_app(settings, market_factory=lambda s: FakeMarket())
    paths = set(app.openapi()["paths"])
    for path in ENDPOINTS.values():
        assert path.replace("/v1/", "/v1/market/") in paths, path
