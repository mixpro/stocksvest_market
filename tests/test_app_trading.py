import json
from decimal import Decimal

import pytest
import requests
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.brokers.alpaca import AlpacaBroker, to_order
from stocksvest_app.brokers.base import Broker, BrokerError, BrokerOrderRequest
from stocksvest_app.config import Settings

KEY = {"X-API-Key": "app-key"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    return Settings()


@pytest.fixture
def broker():
    return FakeBroker(cash="10000", positions={"BBB": ("5", "40")})


@pytest.fixture
def client(settings, broker):
    with TestClient(create_app(settings, market_factory=lambda s: FakeMarket(),
                               broker_factory=lambda account: broker)) as c:
        yield c


@pytest.fixture
def portfolio(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Alpaca paper", "provider": "ALPACA"}).json()
    return client.post("/v1/portfolios", headers=KEY, json={"name": "Paper", "brokerAccountId": acct["id"]}).json()


@pytest.fixture
def automation(client, portfolio):
    f = client.post("/v1/filters", headers=KEY, json={"name": "All", "inputs": [{"type": "UNIVERSE"}]}).json()
    return client.post("/v1/automations", headers=KEY, json={
        "name": "Bot", "portfolioId": portfolio["id"], "runTargets": [{"type": "FILTER", "id": f["id"]}],
        "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}],
        "actions": [{"type": "NOTIFY", "title": "x"}]}).json()


def buy(client, portfolio, **kw):
    body = {"symbol": "AAA", "side": "BUY", "quantity": 10, **kw}
    return client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json=body)


def sync(client, portfolio):
    return client.post(f"/v1/portfolios/{portfolio['id']}/sync", headers=KEY).json()


def ledger(client, automation):
    return [(e["type"], e["amount"]) for e in reversed(client.get(
        f"/v1/automations/{automation['id']}/budget", headers=KEY).json()["entries"])]


# ------------------------------------------------------------------ broker accounts


def test_broker_accounts(client):
    r = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"})
    assert r.status_code == 201
    acct = r.json()
    assert acct["status"] == "CONNECTED" and acct["environment"] == "PAPER"
    assert acct["snapshot"]["cash"] == 10000 and "GTD" not in acct["capabilities"]["timeInForce"]
    live = client.post("/v1/broker-accounts", headers=KEY, json={"name": "x", "provider": "ALPACA", "environment": "LIVE"})
    assert live.status_code == 422 and "live trading is not enabled" in live.text
    assert "not available yet" in client.post("/v1/broker-accounts", headers=KEY, json={"name": "x", "provider": "IBKR"}).text
    assert isinstance(FakeBroker(), Broker)


def test_broker_error_marks_account(settings):
    class Down(FakeBroker):
        def account_snapshot(self):
            raise BrokerError("Alpaca unreachable", retryable=True)

    with TestClient(create_app(settings, market_factory=lambda s: FakeMarket(),
                               broker_factory=lambda a: Down())) as c:
        acct = c.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
        assert acct["status"] == "ERROR" and "unreachable" in acct["lastError"]


# ------------------------------------------------------------------ portfolios and manual orders


def test_portfolio_adopts_opening_positions(client, portfolio):
    assert portfolio["cash"] == 10000 and portfolio["unallocatedCash"] == 10000
    assert [(p["symbol"], p["quantity"], p["automationId"]) for p in portfolio["positions"]] == [("BBB", 5, None)]
    assert portfolio["lastSync"]["mismatches"] == []
    again = client.post("/v1/portfolios", headers=KEY, json={"name": "x", "brokerAccountId": portfolio["brokerAccountId"]})
    assert again.status_code == 409


def test_manual_buy_fill_and_sync(client, portfolio, broker):
    assert buy(client, portfolio, symbol="ZZZ").status_code == 422  # not halal / unknown
    assert "INSUFFICIENT_BUDGET" in buy(client, portfolio, quantity=5000).text  # 5000 * 10 > 10000
    assert "ZERO_QUANTITY" in buy(client, portfolio, quantity=1.5).text  # whole shares only

    order = buy(client, portfolio).json()
    assert order["status"] == "SUBMITTED" and order["estimatedPrice"] == 10.0 and order["automationId"] is None
    assert broker.submitted[-1].client_order_id == order["clientOrderId"]

    broker.fill(order["brokerOrderId"], 10, "10.05")
    out = sync(client, portfolio)
    assert out["sync"]["fillsImported"] == 1 and out["sync"]["mismatches"] == []
    got = client.get(f"/v1/orders/{order['id']}", headers=KEY).json()
    assert got["status"] == "FILLED" and got["averageFillPrice"] == 10.05
    assert got["slippage"] == pytest.approx(0.05) and got["fills"][0]["quantity"] == 10
    pf = out["portfolio"]
    assert pf["cash"] == pytest.approx(10000 - 100.5)
    aaa = next(p for p in pf["positions"] if p["symbol"] == "AAA")
    assert aaa["quantity"] == 10 and aaa["averageCost"] == 10.05 and aaa["marketValue"] == 100.0
    assert sync(client, portfolio)["sync"]["fillsImported"] == 0  # imported once


def test_order_validation_against_broker_capabilities(client, portfolio):
    assert "NOT_SUPPORTED_BY_BROKER" in buy(client, portfolio, timeInForce="GTD").text
    assert "LIMIT + DAY" in buy(client, portfolio, extendedHours=True).text
    assert "needs a limit price" in buy(client, portfolio, type="LIMIT").text
    assert "NOT_HELD" in client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY,
                                     json={"symbol": "AAA", "side": "SELL", "quantity": 1}).text


def test_broker_rejection_and_cancel(client, portfolio, broker):
    broker.fail_next = BrokerError("Alpaca refused (403): insufficient buying power")
    rejected = buy(client, portfolio).json()
    assert rejected["status"] == "REJECTED" and "insufficient" in rejected["rejectReason"]

    order = buy(client, portfolio, type="LIMIT", limitPrice=9.5).json()
    cancelled = client.post(f"/v1/orders/{order['id']}/cancel", headers=KEY).json()
    assert cancelled["status"] == "CANCELLED" and cancelled["cancelledAt"]
    assert client.post(f"/v1/orders/{order['id']}/cancel", headers=KEY).status_code == 409


def test_unreachable_broker_leaves_order_pending_then_sync_finds_it(client, portfolio, broker):
    broker.fail_next = BrokerError("timeout", retryable=True)
    order = buy(client, portfolio).json()
    assert order["status"] == "PENDING"
    # The broker did receive it after all:
    from stocksvest_app.brokers.base import BrokerOrderRequest as Req
    broker.submit_order(Req(order["clientOrderId"], "AAA", "BUY", "MARKET", Decimal(10), "DAY"))
    sync(client, portfolio)
    assert client.get(f"/v1/orders/{order['id']}", headers=KEY).json()["status"] == "SUBMITTED"


def test_position_mismatch_is_reported(client, portfolio, broker):
    broker.held["CCC"] = (Decimal(3), Decimal(5))  # bought outside the app, no fill visible
    mismatches = sync(client, portfolio)["sync"]["mismatches"]
    assert mismatches == [{"kind": "POSITION_QUANTITY", "symbol": "CCC", "appValue": 0.0, "brokerValue": 3.0}]


# ------------------------------------------------------------------ budgets


def test_budget_allocate_and_withdraw(client, portfolio, automation):
    url = f"/v1/automations/{automation['id']}/budget"
    assert client.post(f"{url}/allocate", headers=KEY, json={"amount": 20000}).status_code == 422  # more than cash
    pool = client.post(f"{url}/allocate", headers=KEY, json={"amount": 3000, "note": "start"}).json()["pool"]
    assert pool["cash"] == 3000 and pool["available"] == 3000 and pool["allocated"] == 3000
    pf = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()
    assert pf["unallocatedCash"] == 7000
    assert "INSUFFICIENT_BUDGET" in buy(client, portfolio, quantity=750).text  # 7500 > 7000 unallocated
    pool = client.post(f"{url}/withdraw", headers=KEY, json={"amount": 1000}).json()["pool"]
    assert pool["cash"] == 2000 and pool["allocated"] == 2000
    assert client.post(f"{url}/withdraw", headers=KEY, json={"amount": 5000}).status_code == 422
    events = [e["type"] for e in client.get(f"/v1/automations/{automation['id']}/events", headers=KEY).json()["events"]]
    assert events[:2] == ["BUDGET_WITHDRAWN", "BUDGET_ALLOCATED"]


def test_automation_buy_and_sell_flow_through_the_ledger(client, portfolio, automation, broker):
    aid = automation["id"]
    client.post(f"/v1/automations/{aid}/budget/allocate", headers=KEY, json={"amount": 1000})

    order = buy(client, portfolio, quantity=50, automationId=aid).json()  # 50 * 10 * 1.01 = 505 reserved
    pool = client.get(f"/v1/automations/{aid}/budget", headers=KEY).json()["pool"]
    assert pool["reserved"] == 505 and pool["available"] == 495
    assert "INSUFFICIENT_BUDGET" in buy(client, portfolio, quantity=50, automationId=aid).text

    broker.fill(order["brokerOrderId"], 30, 10)  # partial
    sync(client, portfolio)
    broker.fill(order["brokerOrderId"], 20, "10.10")
    sync(client, portfolio)
    pool = client.get(f"/v1/automations/{aid}/budget", headers=KEY).json()["pool"]
    assert pool["cash"] == pytest.approx(1000 - 300 - 202)
    assert pool["reserved"] == 0 and pool["invested"] == pytest.approx(502)
    assert ledger(client, {"id": aid}) == [("ALLOCATE", 1000), ("RESERVE", 505), ("BUY_FILL", -300),
                                           ("BUY_FILL", -202), ("RELEASE", -3)]

    # Manual shares (BBB opening) are not the automation's to sell.
    assert "NOT_HELD" in client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "BBB", "side": "SELL", "quantity": 1, "automationId": aid}).text
    sell = client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "SELL", "quantity": 40, "automationId": aid}).json()
    assert "NOT_HELD" in client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "SELL", "quantity": 11, "automationId": aid}).text  # 40 already being sold
    broker.fill(sell["brokerOrderId"], 40, 11, fee="1")
    sync(client, portfolio)
    pool = client.get(f"/v1/automations/{aid}/budget", headers=KEY).json()["pool"]
    # Sold the 30 @10 lot and 10 of the 20 @10.10 lot at 11: P&L = 30*1 + 10*0.9 = 39
    assert pool["realizedPnL"] == pytest.approx(39) and pool["fees"] == 1
    assert pool["invested"] == pytest.approx(101) and pool["cash"] == pytest.approx(498 + 440 - 1)
    lots = client.get(f"/v1/portfolios/{portfolio['id']}/lots", headers=KEY).json()["lots"]
    assert [(l["symbol"], l["remainingQuantity"], l["automationId"]) for l in lots] == [("BBB", 5, None), ("AAA", 10, aid)]


# ------------------------------------------------------------------ Alpaca adapter


class Recorder:
    def __init__(self, responses):
        self.headers, self.calls, self.responses = {}, [], list(responses)

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        status, body = self.responses.pop(0)
        r = requests.Response()
        r.status_code, r._content = status, (__import__("json").dumps(body).encode() if body is not None else b"")
        return r


ALPACA_ORDER = {"id": "a1", "client_order_id": "sv_1", "symbol": "AAPL", "side": "buy", "type": "limit",
                "status": "partially_filled", "qty": "10", "filled_qty": "4", "filled_avg_price": "150.25",
                "submitted_at": "2026-10-19T14:00:00Z", "updated_at": "2026-10-19T14:01:00.123456Z"}


def test_alpaca_order_mapping():
    o = to_order(ALPACA_ORDER)
    assert (o.status, o.raw_status, o.side, o.type) == ("PARTIALLY_FILLED", "partially_filled", "BUY", "LIMIT")
    assert o.filled_quantity == Decimal(4) and o.average_fill_price == Decimal("150.25")
    assert to_order({**ALPACA_ORDER, "status": "replaced"}).status == "CANCELLED"
    assert to_order({**ALPACA_ORDER, "status": "done_for_day"}).status == "SUBMITTED"


def test_alpaca_submit_and_duplicate_client_id():
    rec = Recorder([(200, ALPACA_ORDER)])
    b = AlpacaBroker("k", "s", session=rec)
    b.submit_order(BrokerOrderRequest("sv_1", "AAPL", "BUY", "LIMIT", Decimal(10), "DAY", limit_price=Decimal("150.5")))
    method, url, _, body = rec.calls[0]
    assert (method, url) == ("POST", "https://paper-api.alpaca.markets/v2/orders")
    assert body == {"client_order_id": "sv_1", "symbol": "AAPL", "qty": "10", "side": "buy", "type": "limit",
                    "time_in_force": "day", "extended_hours": False, "limit_price": "150.5"}
    assert rec.headers["APCA-API-KEY-ID"] == "k"

    dup = Recorder([(422, {"message": "client_order_id must be unique"}), (200, ALPACA_ORDER)])
    got = AlpacaBroker("k", "s", session=dup).submit_order(
        BrokerOrderRequest("sv_1", "AAPL", "BUY", "MARKET", Decimal(10), "DAY"))
    assert got.broker_order_id == "a1" and dup.calls[1][1].endswith("/v2/orders:by_client_order_id")

    refused = Recorder([(403, {"message": "insufficient buying power"})])
    with pytest.raises(BrokerError) as e:
        AlpacaBroker("k", "s", session=refused).submit_order(
            BrokerOrderRequest("sv_2", "AAPL", "BUY", "MARKET", Decimal(10), "DAY"))
    assert not e.value.retryable and "insufficient" in str(e.value)


def test_alpaca_fills_pagination_and_live_refused(monkeypatch):
    page1 = [{"id": f"x{i}", "order_id": "a1", "symbol": "AAPL", "side": "buy", "qty": "1", "price": "10",
              "transaction_time": "2026-10-19T14:00:00Z"} for i in range(100)]
    page2 = [{**page1[0], "id": "y1", "side": "sell"}]
    rec = Recorder([(200, page1), (200, page2)])
    fills = AlpacaBroker("k", "s", session=rec).list_fills(None)
    assert len(fills) == 101 and fills[-1].side == "SELL" and rec.calls[1][2]["page_token"] == "x99"

    from stocksvest_app.brokers.registry import make_broker
    with pytest.raises(BrokerError, match="live trading is not enabled"):
        make_broker({"environment": "LIVE", "provider": "ALPACA", "credentialsRef": "ALPACA"})
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(BrokerError, match="NOPE_KEY"):
        make_broker({"environment": "PAPER", "provider": "ALPACA", "credentialsRef": "NOPE"})
