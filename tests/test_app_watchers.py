"""Watchers: holding drops (extended hours), market and sector drops (regular session), each key once, pushes,
errors, and the /v1/watchers and /v1/events routes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from fake_market import FakeMarket
from stocksvest_app import db
from stocksvest_app.alerts import push
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings
from stocksvest_app.deps import AppContext
from stocksvest_app.watchers import store, thread
from test_app_push import FakeExpo

ET = ZoneInfo("America/New_York")
KEY = {"X-API-Key": "app-key"}


class Market(FakeMarket):
    """Snapshots dated `day`, with the daily changes the test sets."""

    def __init__(self, day: str, changes: dict[str, float]):
        super().__init__()
        self.day, self.changes, self.broken = day, changes, False

    def snapshots(self, symbols=None):
        if self.broken:
            raise RuntimeError("snapshots down")
        out = {}
        for t, ch in self.changes.items():
            if symbols is None or t in symbols:
                out[t] = {"latestPrice": 100 + ch, "latestTime": f"{self.day}T13:00:00Z", "dailyClose": 100.0,
                          "dailyDate": "2026-10-30", "previousClose": 100.0, "dailyChangePercent": ch}
        return {"snapshots": out}

    def quotes(self, symbols):
        """Live last trades set in self.live; others only have the minute bar (source DELAYED)."""
        live = getattr(self, "live", {})
        return {"quotes": {t: {"source": "ALPACA" if t in live else "DELAYED", "bid": None, "ask": None,
                               "last": live.get(t, 100 + self.changes[t]), "time": f"{self.day}T13:01:00Z"}
                           for t in symbols if t in self.changes}}


def setup(tmp_path, monkeypatch, market):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    expo = FakeExpo()
    monkeypatch.setattr(push.requests, "post", expo.post)
    ctx = AppContext(Settings(), market)
    conn = db.connect(ctx.settings.db_path)
    db.init(conn)
    conn.execute("INSERT INTO broker_accounts(id, name, provider, environment, credentials_ref, status, created_at, updated_at) "
                 "VALUES ('bka_1', 'Paper', 'ALPACA', 'PAPER', 'x', 'CONNECTED', 't', 't')")
    conn.execute("INSERT INTO portfolios(id, name, broker_account_id, currency, created_at, updated_at) "
                 "VALUES ('pf_1', 'Core 40', 'bka_1', 'USD', 't', 't')")
    conn.execute("INSERT INTO position_lots(id, portfolio_id, symbol, source, quantity, remaining_quantity, "
                 "cost_per_share, opened_at) VALUES ('lot_1', 'pf_1', 'BBB', 'FILL', '5', '5', '10', 't')")
    conn.commit()
    push._put(conn, "push:devices", {"tokens": ["ExponentPushToken[abc]"]})
    return ctx, conn, expo


def at(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=ET)


def test_holding_drop_in_premarket_fires_once_per_step_and_pushes(tmp_path, monkeypatch):
    market = Market("2026-11-02", {"AAA": 3.0, "BBB": -16.7, "CCC": 0.0})
    ctx, conn, expo = setup(tmp_path, monkeypatch, market)

    assert len(thread.tick(ctx, conn, at("2026-11-02", "08:00"))) == 1  # pre-market: the market watcher waits
    [ev] = store.list_events(conn)
    assert ev["key"] == "drop:BBB:2026-11-02:2" and ev["kind"] == "HOLDING_DROP" and ev["severity"] == 4
    assert ev["status"] == "DONE" and "Core 40" in ev["body"]
    assert [m["title"] for m in expo.sent] == ["BBB down 16.7%"] and expo.sent[0]["data"]["category"] == "events"

    assert thread.tick(ctx, conn, at("2026-11-02", "08:02")) == []  # same step again: nothing new
    market.changes["BBB"] = -25.0
    assert len(thread.tick(ctx, conn, at("2026-11-02", "08:04"))) == 1  # next step (24%) fires
    market.live = {"BBB": 65.0}  # the live last trade wins over the minute bar
    assert len(thread.tick(ctx, conn, at("2026-11-02", "08:06"))) == 1
    assert store.list_events(conn)[0]["key"] == "drop:BBB:2026-11-02:4"
    assert thread.tick(ctx, conn, at("2026-11-02", "21:00")) == []  # after 8pm: outside its hours
    assert thread.tick(ctx, conn, at("2026-11-01", "10:00")) == []  # Sunday: market closed


def test_market_and_sector_drop_in_regular_session(tmp_path, monkeypatch):
    market = Market("2026-11-02", {"AAA": -4.0, "BBB": 1.0, "CCC": -3.5, "DDD": -5.0})
    ctx, conn, _ = setup(tmp_path, monkeypatch, market)
    store.save_config(conn, thread.holdings_drop, enabled=False)
    store.save_config(conn, thread.market, settings={"minSectorStocks": 2})

    thread.tick(ctx, conn, at("2026-11-02", "11:00"))
    events = {e["key"]: e for e in store.list_events(conn)}
    assert set(events) == {"market:2026-11-02:1", "sector:TECHNOLOGY:2026-11-02:1"}  # median −3.75; tech −3.75
    assert events["sector:TECHNOLOGY:2026-11-02:1"]["title"] == "Technology down 3.8% today"
    assert thread.tick(ctx, conn, at("2026-11-02", "11:02")) == []  # not due yet (every 5 minutes)


def test_failing_watcher_records_its_error_once_and_others_still_run(tmp_path, monkeypatch):
    market = Market("2026-11-02", {"BBB": -9.0})
    ctx, conn, _ = setup(tmp_path, monkeypatch, market)
    market.broken = True
    thread.tick(ctx, conn, at("2026-11-02", "11:00"))
    thread.tick(ctx, conn, at("2026-11-02", "11:10"))
    assert store.config(conn, thread.holdings_drop)["lastError"] == "snapshots down"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'WATCHER_ERROR'").fetchone()[0] == sum(
        bool(store.config(conn, w)["lastError"]) for w in thread.WATCHERS.values())  # once per failing watcher
    market.broken = False
    thread.tick(ctx, conn, at("2026-11-02", "11:20"))
    assert store.config(conn, thread.holdings_drop)["lastError"] is None
    assert {e["key"] for e in store.list_events(conn)} == {"drop:BBB:2026-11-02:1", "market:2026-11-02:4"}


def test_watcher_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket())) as c:
        ws = {w["name"]: w for w in c.get("/v1/watchers", headers=KEY).json()["watchers"]}
        assert ws["holdings-drop"]["enabled"] and ws["holdings-drop"]["settings"] == {"dropPercent": 8.0}
        w = c.put("/v1/watchers/holdings-drop", json={"enabled": False, "settings": {"dropPercent": 10}}, headers=KEY).json()
        assert w["enabled"] is False and w["settings"] == {"dropPercent": 10.0}
        assert c.put("/v1/watchers/holdings-drop", json={"enabled": True}, headers=KEY).json()["settings"]["dropPercent"] == 10
        assert c.put("/v1/watchers/nope", json={}, headers=KEY).status_code == 404
        assert c.put("/v1/watchers/market", json={"settings": {"bogus": 1}}, headers=KEY).status_code == 422
        assert c.put("/v1/watchers/market", json={"settings": {"marketDropPercent": 0}}, headers=KEY).status_code == 422
        assert c.get("/v1/events", headers=KEY).json() == {"events": []}
