"""Backtests: the as-of market (no look-ahead) and a full replay of a Buy and a Sell automation on a tiny market."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.backtest import engine
from stocksvest_app.backtest.history import HistoricalMarket, spread_for
from stocksvest_app.config import Settings
from stocksvest_market import db as market_db

ET = ZoneInfo("America/New_York")
KEY = {"X-API-Key": "app-key"}
DAYS = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
PATHS = {"UP": 1.01, "DOWN": 0.99}  # price factor per hour


def at(day: str, hour: int) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(hour), ET)


@pytest.fixture
def market_path(tmp_path):
    """Two liquid stocks over five trading days, hourly bars 04:00-20:00 New York: UP rises 1% an hour, DOWN falls 1%."""
    path = tmp_path / "market.db"
    conn = market_db.connect(path)
    market_db.init(conn)
    for t in PATHS:
        conn.execute("INSERT INTO symbols(id, ticker, name, exchange, asset_type, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'NASDAQ', 'COMMON_STOCK', 'x', 'x')", (f"id-{t}", t, f"{t} Inc"))
    for day in DAYS:
        conn.execute("INSERT INTO calendar VALUES (?, ?, ?, ?, ?)", (day, f"{day}T04:00:00-04:00",
                     f"{day}T09:30:00-04:00", f"{day}T16:00:00-04:00", f"{day}T20:00:00-04:00"))
    for t, factor in PATHS.items():
        price = 100.0
        for day in DAYS:
            for hour in range(4, 20):
                o, price = price, price * factor
                conn.execute("INSERT INTO bars_1hour VALUES (?, ?, ?, ?, ?, ?, 1000000, 100, ?)",
                             (f"id-{t}", int(at(day, hour).timestamp()), o, max(o, price), min(o, price), price, price))
            conn.execute("INSERT INTO bars_1day VALUES (?, ?, ?, ?, ?, ?, 16000000, 1600, ?)",
                         (f"id-{t}", int(at(day, 0).timestamp()), 1, 1, 1, price, price))
    conn.commit()
    conn.close()
    return path


def hourly_close(day, hour, factor):
    """UP/DOWN close of the bar starting at `hour` on `day` (16 bars a day from 04:00)."""
    n = DAYS.index(day) * 16 + (hour - 4) + 1
    return 100.0 * factor ** n


def test_as_of_prices_never_look_ahead(market_path):
    m = HistoricalMarket(market_path, date(2026, 10, 5), date(2026, 10, 7))
    m.now = at("2026-10-06", 10)
    snap = m.snapshots(["UP"])["snapshots"]["UP"]
    assert snap["latestPrice"] == pytest.approx(hourly_close("2026-10-06", 9, 1.01))  # the 09:00 bar, not 10:00's
    assert snap["previousClose"] == pytest.approx(hourly_close("2026-10-05", 19, 1.01))  # Monday's close
    one_day = m.price_change(1, "DAYS", ["UP"])["changes"]["UP"]
    assert one_day["fromPrice"] == pytest.approx(snap["previousClose"])
    three_h = m.price_change(3, "HOURS", ["DOWN"])["changes"]["DOWN"]
    assert three_h["changePercent"] == pytest.approx((0.99 ** 3 - 1) * 100)
    m.now = at("2026-10-06", 15)  # Tuesday's daily bar isn't complete until 20:00
    assert m.snapshots(["UP"])["snapshots"]["UP"]["previousClose"] == pytest.approx(snap["previousClose"])
    q = m.quotes(["UP"])["quotes"]["UP"]
    assert q["bid"] < q["last"] < q["ask"] and q["source"] == "IBKR"
    vol = m.volume(2, ["UP"])["volumes"]["UP"]
    assert vol["averageVolume"] == 16_000_000 and vol["daysTraded"] == 2  # Mon + Fri, today not finished


def test_spread_model_widens_for_thin_stocks():
    assert spread_for(1e9) < spread_for(1e7) < spread_for(1e5) and spread_for(None) == 10.0


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    """An app DB with a Buy (everything, $1,000 each) and a Sell (unrealized below -1%) in one $10,000 budget."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    (tmp_path / "app").mkdir()
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket(),
                               broker_factory=lambda a: FakeBroker(cash="20000"))) as c:
        acct = c.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
        pf = c.post("/v1/portfolios", headers=KEY,
                    json={"name": "Paper", "brokerAccountId": acct["id"], "capital": 10000}).json()
        everything = c.post("/v1/filters", headers=KEY, json={"name": "All", "inputs": [{"type": "UNIVERSE"}]}).json()
        losing = c.post("/v1/filters", headers=KEY, json={
            "name": "Stop", "inputs": [{"type": "INVESTMENT_PORTFOLIO", "id": pf["id"]}],
            "actions": [{"type": "UNREALIZED_PNL", "portfolioIds": [pf["id"]], "valueType": "PERCENT",
                         "condition": "LESS_THAN", "value": -1}]}).json()
        schedule = [{"type": "MARKET", "anchor": "REGULAR_OPEN", "repeatEveryMinutes": 15}]
        ids = {}
        for name, flt, action, guards in (
                ("Buy", everything, {"type": "PLACE_ORDER", "side": "BUY",
                                     "sizings": [{"mode": "CASH_AMOUNT", "value": 1000}]},
                 {"rebuyCooldownMinutes": 600}),
                ("Sell", losing, {"type": "PLACE_ORDER", "side": "SELL",
                                  "sizings": [{"mode": "PERCENT_OF_POSITION", "value": 100}]}, {})):
            a = c.post("/v1/automations", headers=KEY, json={
                "name": name, "runTargets": [{"type": "FILTER", "id": flt["id"]}], "schedules": schedule,
                "actions": [action], "orderGuards": guards}).json()
            ids[name] = a["id"]
        r = c.post(f"/v1/portfolios/{pf['id']}/budgets", headers=KEY,
                   json={"name": "Test", "amount": 10000, "automationIds": list(ids.values())})
        assert r.status_code == 201, r.text
    return Settings().db_path, ids


def test_replay_buys_sells_and_respects_the_cooldown(market_path, app_db):
    path, ids = app_db
    out = engine.run(path, market_path, ids["Buy"], ids["Sell"], date(2026, 10, 5), date(2026, 10, 7))
    trades = out["trades"]
    downs = [t for t in trades if t["symbol"] == "DOWN"]
    assert [t["side"] for t in downs[:2]] == ["BUY", "SELL"] and downs[1]["filter"] == "Stop" and downs[1]["pnl"] < 0
    first_sell = datetime.fromisoformat(downs[1]["time"])
    rebuys = [t for t in downs[2:] if t["side"] == "BUY"]
    assert all(datetime.fromisoformat(t["time"]) - first_sell >= timedelta(minutes=600) for t in rebuys)
    ups = [t for t in trades if t["symbol"] == "UP"]
    assert [t["side"] for t in ups] == ["BUY"]  # bought once, kept (skip buy if held), never stopped out
    s = out["summary"]
    assert s["tradingDays"] == 3 and len(out["equity"]) == 3 and s["bySellFilter"]["Stop"]["wins"] == 0
    assert out["equity"][-1]["equity"] == pytest.approx(s["finalEquity"])
    up, down = 1.01 ** 32, 0.99 ** 32  # Mon close -> Wed close, 16 bars a day: (UP + DOWN) / 2 - 1
    assert s["buyHold"]["allPct"] == pytest.approx(((up + down) / 2 - 1) * 100, abs=0.01)


def test_overrides_change_the_run(market_path, app_db):
    path, ids = app_db
    base = engine.run(path, market_path, ids["Buy"], ids["Sell"], date(2026, 10, 5), date(2026, 10, 7))
    capped = engine.run(path, market_path, ids["Buy"], ids["Sell"], date(2026, 10, 5), date(2026, 10, 7),
                        overrides={"buy": {"orderGuards": {"maxSpreadPercent": 0.0001}}})
    assert base["summary"]["buys"] > 0 and capped["summary"]["buys"] == 0  # every spread is over the cap


def test_next_vwap_fills_after_the_signal(market_path, app_db):
    path, ids = app_db
    m = HistoricalMarket(market_path, date(2026, 10, 5), date(2026, 10, 7))
    m.now = at("2026-10-06", 10)
    half = spread_for(m.avg_dollar_volume("UP")) / 200
    assert m.next_fill("UP", "BUY") == pytest.approx(hourly_close("2026-10-06", 10, 1.01) * (1 + half), rel=1e-6)
    m.now = at("2026-10-06", 20)  # nothing trades after the extended close
    assert m.next_fill("UP", "BUY") is None
    nxt = engine.run(path, market_path, ids["Buy"], ids["Sell"], date(2026, 10, 5), date(2026, 10, 5))
    same = engine.run(path, market_path, ids["Buy"], ids["Sell"], date(2026, 10, 5), date(2026, 10, 5),
                      fill="SAME_CLOSE")
    up = [next(t for t in r["trades"] if t["symbol"] == "UP") for r in (nxt, same)]
    assert up[0]["price"] == pytest.approx(up[1]["price"] * 1.01, rel=1e-6)  # one hour later on a +1%/hour stock


def test_schedule_steps_run_each_automation_at_its_own_times(market_path, app_db):
    path, ids = app_db
    weekly = [{"type": "WEEKLY", "onDays": ["TUE"], "atTimes": ["11:00:00"]}]
    out = engine.run(path, market_path, ids["Buy"], ids["Sell"], date(2026, 10, 5), date(2026, 10, 7),
                     overrides={"buy": {"schedules": weekly}})
    buys = [t for t in out["trades"] if t["side"] == "BUY"]
    assert buys and {datetime.fromisoformat(t["time"]).astimezone(ET).strftime("%a %H:%M")
                     for t in buys} == {"Tue 11:00"}
    every_15 = engine._fire_times({"schedules": [{"type": "MARKET", "anchor": "REGULAR_OPEN",
                                                  "repeatEveryMinutes": 15}], "runTargets": [], "actions": [],
                                   "name": "x"},
                                  date(2026, 10, 5), lambda a, b: HistoricalMarket(market_path, a, b).calendar(
                                      a.isoformat(), b.isoformat())["days"], at("2026-10-05", 0))
    assert [t.astimezone(ET).strftime("%H:%M") for t in every_15] == [
        "09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]  # 09:30 runs at the hour's start
