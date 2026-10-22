from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings
from stocksvest_app.performance import snapshots

KEY = {"X-API-Key": "app-key"}
ET = ZoneInfo("America/New_York")


@pytest.fixture
def broker():
    return FakeBroker(cash="10000")


@pytest.fixture
def app_and_client(tmp_path, monkeypatch, broker):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    app = create_app(Settings(), market_factory=lambda s: FakeMarket(), broker_factory=lambda a: broker)
    with TestClient(app) as c:
        yield app, c


@pytest.fixture
def client(app_and_client):
    return app_and_client[1]


@pytest.fixture
def portfolio(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    return client.post("/v1/portfolios", headers=KEY,
                       json={"name": "P", "brokerAccountId": acct["id"], "capital": 5000}).json()


@pytest.fixture
def budget(client, portfolio):
    return client.post(f"/v1/portfolios/{portfolio['id']}/budgets", headers=KEY,
                       json={"name": "Main", "amount": 1000}).json()


def buy_and_fill(client, broker, portfolio, budget_id=None, qty=10, price=8):
    o = client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "BUY", "quantity": qty, "budgetId": budget_id}).json()
    broker.fill(o["brokerOrderId"], qty, price)
    client.post(f"/v1/portfolios/{portfolio['id']}/sync", headers=KEY)


def ctx_conn(app_and_client):
    """A context and DB connection on the same data, for calling the scheduled job directly."""
    from stocksvest_app import db
    from stocksvest_app.deps import AppContext
    settings = Settings()
    return AppContext(settings, FakeMarket()), db.connect(settings.db_path)


def test_portfolio_history_values(client, broker, portfolio, budget):
    buy_and_fill(client, broker, portfolio, budget["id"])  # 10 AAA @ 8, now priced at 10
    snap = client.post(f"/v1/portfolios/{portfolio['id']}/snapshot", headers=KEY).json()
    assert snap["kind"] == "MANUAL"
    assert (snap["cash"], snap["positionsValue"], snap["equity"]) == (4920.0, 100.0, 5020.0)
    assert (snap["invested"], snap["unrealizedPnL"], snap["allocated"]) == (80.0, 20.0, 5000.0)
    assert snap["unallocated"] == 4000.0  # 4920 cash - 920 still in the budget
    hist = client.get(f"/v1/portfolios/{portfolio['id']}/history", headers=KEY).json()
    assert [p["kind"] for p in hist["points"]] == ["MANUAL"] and hist["current"]["equity"] == 5020.0
    for p in (hist["points"][0], hist["current"]):  # +20 on 5,000 capital; +20 on 80 of cost
        assert (p["totalReturn"], p["totalReturnPct"], p["unrealizedPct"]) == (20.0, 0.4, 25.0)
    assert client.get(f"/v1/portfolios/{portfolio['id']}/history?kind=CLOSE", headers=KEY).json()["points"] == []


def test_budget_performance(client, broker, portfolio, budget):
    buy_and_fill(client, broker, portfolio, budget["id"])
    perf = client.get(f"/v1/budgets/{budget['id']}/performance", headers=KEY).json()
    cur = perf["current"]
    assert (cur["cash"], cur["invested"], cur["positionsValue"], cur["equity"]) == (920.0, 80.0, 100.0, 1020.0)
    assert cur["returnPercent"] == pytest.approx(2.0) and cur["openPositions"] == 1
    assert perf["points"] == []


def test_scheduled_snapshots(app_and_client, client, broker, portfolio, budget):
    ctx, conn = ctx_conn(app_and_client)
    buy_and_fill(client, broker, portfolio, budget["id"])

    def at(h, m):  # Monday 12 Oct 2026, New York time (in the past: fills happen on the real clock)
        return datetime(2026, 10, 12, h, m, tzinfo=ET).astimezone(timezone.utc)

    for t in (at(9, 0), at(9, 31), at(9, 40), at(9, 46), at(15, 59), at(16, 10)):
        snapshots.scheduled(ctx, conn, t)
    hist = client.get(f"/v1/portfolios/{portfolio['id']}/history", headers=KEY).json()["points"]
    assert [p["kind"] for p in hist] == ["INTERVAL", "INTERVAL", "INTERVAL"]  # 9:31 and 9:46 slots, 15:59

    snapshots.scheduled(ctx, conn, at(16, 25))
    snapshots.scheduled(ctx, conn, at(17, 0))  # once per day
    closes = client.get(f"/v1/portfolios/{portfolio['id']}/history?kind=CLOSE", headers=KEY).json()["points"]
    assert len(closes) == 1 and closes[0]["date"] == "2026-10-12"
    perf = client.get(f"/v1/budgets/{budget['id']}/performance", headers=KEY).json()["points"]
    assert [(p["date"], p["equity"], p["fills"]) for p in perf] == [("2026-10-12", 1020.0, 0)]

    snapshots.scheduled(ctx, conn, datetime(2026, 10, 24, 12, 0, tzinfo=ET))  # Saturday: nothing
    assert len(client.get(f"/v1/portfolios/{portfolio['id']}/history", headers=KEY).json()["points"]) == 4


def test_equity_history_is_cached(client, portfolio, broker):
    url = f"/v1/broker-accounts/{portfolio['brokerAccountId']}/equity-history?period=1M"
    first = client.get(url, headers=KEY).json()
    assert first["cached"] is False and first["points"][1]["equity"] == 100000.0
    assert client.get(url, headers=KEY).json()["cached"] is True and broker.history_calls == 1
    assert client.get(url.replace("1M", "1X"), headers=KEY).status_code == 422


def test_alpaca_equity_history_mapping():
    from test_app_trading import Recorder
    from stocksvest_app.brokers.alpaca import AlpacaBroker
    day = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    rec = Recorder([(200, {"timestamp": [day - 86400, day, day + 86400], "equity": [0, 100000.0, None],
                           "profit_loss": [0, 0.0, None], "timeframe": "1D"})])  # 0 = before the account existed
    points = AlpacaBroker("k", "s", session=rec).equity_history("1M", "1D")
    assert points == [{"at": "2026-10-15T00:00:00Z", "equity": 100000.0, "profitLoss": 0.0}]
    assert rec.calls[0][1].endswith("/v2/account/portfolio/history") and rec.calls[0][2] == {"period": "1M", "timeframe": "1D"}
