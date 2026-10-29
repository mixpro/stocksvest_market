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
def market():
    return FakeMarket()


@pytest.fixture
def client(settings, broker, market):
    with TestClient(create_app(settings, market_factory=lambda s: market,
                               broker_factory=lambda account: broker)) as c:
        yield c


@pytest.fixture
def portfolio(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Alpaca paper", "provider": "ALPACA"}).json()
    return client.post("/v1/portfolios", headers=KEY,
                       json={"name": "Paper", "brokerAccountId": acct["id"], "capital": 10000}).json()


@pytest.fixture
def automation(client, portfolio):
    f = client.post("/v1/filters", headers=KEY, json={"name": "All", "inputs": [{"type": "UNIVERSE"}]}).json()
    return client.post("/v1/automations", headers=KEY, json={
        "name": "Bot", "runTargets": [{"type": "FILTER", "id": f["id"]}],
        "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}],
        "actions": [{"type": "NOTIFY", "title": "x"}]}).json()


def buy(client, portfolio, **kw):
    body = {"symbol": "AAA", "side": "BUY", "quantity": 10, **kw}
    return client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json=body)


def sync(client, portfolio):
    return client.post(f"/v1/portfolios/{portfolio['id']}/sync", headers=KEY).json()


@pytest.fixture
def budget(client, portfolio, automation):
    """An empty budget of the portfolio, with the automation in it."""
    return client.post(f"/v1/portfolios/{portfolio['id']}/budgets", headers=KEY,
                       json={"name": "Main", "automationIds": [automation["id"]]}).json()


def ledger(client, budget):
    return [(e["type"], e["amount"]) for e in reversed(client.get(
        f"/v1/budgets/{budget['id']}/ledger", headers=KEY).json()["entries"])]


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


def test_shares_already_in_the_account_stay_unassigned(client, portfolio):
    assert portfolio["cash"] == 10000 and portfolio["unallocatedCash"] == 10000 and portfolio["allocated"] == 10000
    assert portfolio["positions"] == [] and portfolio["lastSync"]["mismatches"] == []
    acct = client.get(f"/v1/broker-accounts/{portfolio['brokerAccountId']}", headers=KEY).json()
    assert acct["unassignedShares"] == {"BBB": 5.0} and acct["unassignedCash"] == 0


def test_several_portfolios_share_one_account(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    new = lambda name, capital: client.post("/v1/portfolios", headers=KEY, json={  # noqa: E731
        "name": name, "brokerAccountId": acct["id"], "capital": capital})
    growth = new("Growth", 4000).json()
    income = new("Income", 3000).json()
    assert growth["account"]["unassignedCash"] == 6000 and income["account"]["unassignedCash"] == 3000
    too_much = new("Big", 3500)
    assert too_much.status_code == 422 and "3000.00" in too_much.text

    url = f"/v1/portfolios/{growth['id']}/capital"
    assert client.post(f"{url}/allocate", headers=KEY, json={"amount": 3001}).status_code == 422
    assert client.post(f"{url}/allocate", headers=KEY, json={"amount": 1000, "note": "more"}).json()["cash"] == 5000
    assert client.post(f"{url}/withdraw", headers=KEY, json={"amount": 5001}).status_code == 422
    assert client.post(f"{url}/withdraw", headers=KEY, json={"amount": 500}).json()["allocated"] == 4500
    ledger = client.get(f"/v1/portfolios/{growth['id']}/ledger", headers=KEY).json()["entries"]
    assert [(e["type"], e["amount"], e["cashAfter"]) for e in reversed(ledger)] == [
        ("ALLOCATE", 4000, 4000), ("ALLOCATE", 1000, 5000), ("WITHDRAW", -500, 4500)]
    overview = client.get(f"/v1/broker-accounts/{acct['id']}", headers=KEY).json()
    assert overview["assignedCash"] == 7500 and overview["unassignedCash"] == 2500
    assert client.delete(f"/v1/portfolios/{income['id']}", headers=KEY).status_code == 204
    assert client.get(f"/v1/broker-accounts/{acct['id']}", headers=KEY).json()["unassignedCash"] == 5500


def test_fills_land_in_the_right_portfolio(client, broker):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    a = client.post("/v1/portfolios", headers=KEY, json={"name": "A", "brokerAccountId": acct["id"], "capital": 2000}).json()
    b = client.post("/v1/portfolios", headers=KEY, json={"name": "B", "brokerAccountId": acct["id"], "capital": 2000}).json()
    order = buy(client, a, quantity=10).json()
    broker.fill(order["brokerOrderId"], 10, 10)
    out = sync(client, b)  # syncing either portfolio syncs the whole account
    assert out["sync"]["fillsImported"] == 1
    got_a = client.get(f"/v1/portfolios/{a['id']}", headers=KEY).json()
    assert got_a["cash"] == 1900 and got_a["positions"][0]["symbol"] == "AAA" and got_a["equity"] == 2000
    assert out["portfolio"]["cash"] == 2000 and out["portfolio"]["positions"] == []
    ledger = client.get(f"/v1/portfolios/{a['id']}/ledger", headers=KEY).json()["entries"]
    assert ledger[0]["type"] == "BUY_FILL" and ledger[0]["amount"] == -100 and ledger[0]["orderId"] == order["id"]


def test_manual_buy_fill_and_sync(client, portfolio, broker):
    assert buy(client, portfolio, symbol="ZZZ").status_code == 422  # not halal / unknown
    assert "INSUFFICIENT_BUDGET" in buy(client, portfolio, quantity=5000).text  # 5000 * 10 > 10000
    assert "ZERO_QUANTITY" in buy(client, portfolio, quantity=1.5, type="LIMIT", limitPrice=10).text  # fractions: MARKET + DAY only

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
    assert pf["cash"] == pytest.approx(10000 - 100.5)  # the portfolio's own cash, from its ledger
    aaa = next(p for p in pf["positions"] if p["symbol"] == "AAA")
    assert aaa["quantity"] == 10 and aaa["averageCost"] == 10.05 and aaa["marketValue"] == 100.0
    assert aaa["unrealizedPnL"] == pytest.approx(-0.5) and aaa["unrealizedPct"] == -0.5  # -0.50 on 100.50
    assert (pf["positionsCost"], pf["positionsValue"], pf["unrealizedPct"]) == (100.5, 100.0, -0.5)
    assert pf["totalReturn"] == pytest.approx(-0.5) and pf["totalReturnPct"] == -0.01  # -0.50 on 10,000 capital
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


def test_a_fill_listed_late_is_not_a_mismatch(client, portfolio, broker):
    order = buy(client, portfolio, quantity=10).json()
    broker.fill(order["brokerOrderId"], 10, 10)
    late = broker.fills.pop()  # Alpaca's balances and order status already show it, its fill list not yet
    out = sync(client, portfolio)["sync"]
    assert out["mismatches"] == [] and out["fillsImported"] == 0
    assert client.get(f"/v1/orders/{order['id']}", headers=KEY).json()["status"] == "SUBMITTED"  # waits for the fill
    broker.fills.append(late)
    out = sync(client, portfolio)
    assert out["sync"]["fillsImported"] == 1 and out["sync"]["mismatches"] == []
    assert client.get(f"/v1/orders/{order['id']}", headers=KEY).json()["status"] == "FILLED"
    assert out["portfolio"]["cash"] == 9900


def test_outside_trades_are_unassigned_and_real_gaps_are_mismatches(client, portfolio, broker):
    broker.held["CCC"] = (Decimal(3), Decimal(5))  # bought outside the app
    out = sync(client, portfolio)["sync"]
    assert out["mismatches"] == [] and out["unassigned"]["shares"] == {"BBB": 5.0, "CCC": 3.0}

    order = buy(client, portfolio, quantity=10).json()
    broker.fill(order["brokerOrderId"], 10, 10)
    sync(client, portfolio)
    broker.held["AAA"] = (Decimal(4), Decimal(10))  # the broker now holds fewer than the portfolio thinks
    mismatches = sync(client, portfolio)["sync"]["mismatches"]
    assert [(m["kind"], m["symbol"], m["appValue"], m["brokerValue"]) for m in mismatches] == [
        ("POSITION_QUANTITY", "AAA", 10.0, 4.0)]


def test_external_fills_are_recorded_once(client, portfolio, broker):
    from datetime import datetime, timezone
    from stocksvest_app.brokers.base import BrokerFill
    broker.fills.append(BrokerFill("ext1", "not-an-app-order", "AAA", "BUY", Decimal(2), Decimal(10), Decimal(0),
                                   datetime.now(timezone.utc)))
    assert sync(client, portfolio)["sync"]["externalFills"] == 1
    assert sync(client, portfolio)["sync"]["externalFills"] == 0
    assert client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()["cash"] == 10000


# ------------------------------------------------------------------ budgets


def test_budget_allocate_and_withdraw(client, portfolio, automation, budget):
    url = f"/v1/budgets/{budget['id']}"
    assert budget["cash"] == 0 and [x["id"] for x in budget["automations"]] == [automation["id"]]
    assert client.post(f"{url}/allocate", headers=KEY, json={"amount": 20000}).status_code == 422  # more than cash
    b = client.post(f"{url}/allocate", headers=KEY, json={"amount": 3000, "note": "start"}).json()
    assert b["cash"] == 3000 and b["available"] == 3000 and b["allocated"] == 3000
    pf = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()
    assert pf["unallocatedCash"] == 7000 and pf["cash"] == 10000  # budgets are part of the portfolio's cash
    assert [x["name"] for x in pf["budgets"]] == ["Main"]
    assert "INSUFFICIENT_BUDGET" in buy(client, portfolio, quantity=750).text  # 7500 > 7000 unallocated
    b = client.post(f"{url}/withdraw", headers=KEY, json={"amount": 1000}).json()
    assert b["cash"] == 2000 and b["allocated"] == 2000
    assert client.post(f"{url}/withdraw", headers=KEY, json={"amount": 5000}).status_code == 422
    assert ledger(client, budget) == [("ALLOCATE", 3000), ("WITHDRAW", -1000)]
    events = [e["type"] for e in client.get(f"/v1/automations/{automation['id']}/events", headers=KEY).json()["events"]]
    assert events[0] == "BUDGET_ADDED"


def test_several_budgets_per_portfolio(client, portfolio, automation, budget):
    base = f"/v1/portfolios/{portfolio['id']}/budgets"
    growth = client.post(base, headers=KEY, json={"name": "Growth", "amount": 4000}).json()
    assert growth["automations"] == [] and growth["cash"] == 4000
    assert client.post(base, headers=KEY, json={"name": "Growth"}).status_code == 409  # names are unique
    assert client.post(base, headers=KEY, json={"name": "X", "amount": 7000}).status_code == 422  # only 6000 left
    assert client.post(base, headers=KEY, json={"name": "X", "automationIds": ["aut_missing"]}).status_code == 422
    assert [b["name"] for b in client.get(base, headers=KEY).json()["budgets"]] == ["Growth", "Main"]
    assert client.put(f"/v1/budgets/{growth['id']}", headers=KEY, json={"name": "Main"}).status_code == 409
    assert client.put(f"/v1/budgets/{growth['id']}", headers=KEY, json={"name": "Long term"}).json()["name"] == "Long term"

    # The portfolio chooses its automations; one automation can be in several budgets.
    added = client.post(f"/v1/budgets/{growth['id']}/automations", headers=KEY, json={"automationId": automation["id"]})
    assert [a["id"] for a in added.json()["automations"]] == [automation["id"]]
    got = client.get(f"/v1/automations/{automation['id']}", headers=KEY).json()["budgets"]
    assert {b["name"] for b in got} == {"Main", "Long term"}
    assert client.delete(f"/v1/budgets/{growth['id']}/automations/{automation['id']}", headers=KEY).json()["automations"] == []
    assert client.delete(f"/v1/budgets/{growth['id']}/automations/{automation['id']}", headers=KEY).status_code == 404

    # A portfolio with budgets can't be deleted; deleting a budget returns its cash.
    assert "delete its budgets first" in client.delete(f"/v1/portfolios/{portfolio['id']}", headers=KEY).text
    assert client.delete(f"/v1/budgets/{growth['id']}", headers=KEY).status_code == 204
    pf = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()
    assert pf["unallocatedCash"] == 10000 and [b["name"] for b in pf["budgets"]] == ["Main"]
    assert ledger(client, growth)[-1] == ("WITHDRAW", -4000)  # the ledger outlives the budget


def test_budget_buy_and_sell_flow_through_the_ledger(client, portfolio, automation, budget, broker):
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})

    order = buy(client, portfolio, quantity=50, budgetId=bid).json()  # 50 * 10 * 1.01 = 505 reserved
    assert order["budgetId"] == bid and order["automationId"] is None
    b = client.get(f"/v1/budgets/{bid}", headers=KEY).json()
    assert b["reserved"] == 505 and b["available"] == 495 and [o["id"] for o in b["openOrders"]] == [order["id"]]
    assert "INSUFFICIENT_BUDGET" in buy(client, portfolio, quantity=50, budgetId=bid).text
    assert "not a budget of this portfolio" in buy(client, portfolio, budgetId="bud_missing").text

    broker.fill(order["brokerOrderId"], 30, 10)  # partial
    sync(client, portfolio)
    broker.fill(order["brokerOrderId"], 20, "10.10")
    sync(client, portfolio)
    b = client.get(f"/v1/budgets/{bid}", headers=KEY).json()
    assert b["cash"] == pytest.approx(1000 - 300 - 202)
    assert b["reserved"] == 0 and b["invested"] == pytest.approx(502)
    assert [(x["symbol"], x["quantity"]) for x in b["positions"]] == [("AAA", 50)]
    assert ledger(client, budget) == [("ALLOCATE", 1000), ("RESERVE", 505), ("BUY_FILL", -300),
                                      ("BUY_FILL", -202), ("RELEASE", -3)]

    # Shares outside the budget (BBB is unassigned on the account) are not its to sell.
    assert "NOT_HELD" in client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "BBB", "side": "SELL", "quantity": 1, "budgetId": bid}).text
    sell = client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "SELL", "quantity": 40, "budgetId": bid}).json()
    assert "NOT_HELD" in client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "SELL", "quantity": 11, "budgetId": bid}).text  # 40 already being sold
    assert "NOT_HELD" in client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "SELL", "quantity": 1}).text  # manual shares: none
    broker.fill(sell["brokerOrderId"], 40, 11, fee="1")
    sync(client, portfolio)
    b = client.get(f"/v1/budgets/{bid}", headers=KEY).json()
    # Sold the 30 @10 lot and 10 of the 20 @10.10 lot at 11: P&L = 30*1 + 10*0.9 = 39
    assert b["realizedPnL"] == pytest.approx(39) and b["fees"] == 1
    assert b["invested"] == pytest.approx(101) and b["cash"] == pytest.approx(498 + 440 - 1)
    lots = client.get(f"/v1/portfolios/{portfolio['id']}/lots", headers=KEY).json()["lots"]
    assert [(l["symbol"], l["remainingQuantity"], l["budgetId"]) for l in lots] == [("AAA", 10, bid)]
    fills = client.get(f"/v1/portfolios/{portfolio['id']}/fills", headers=KEY).json()["fills"]
    assert fills[0]["side"] == "SELL" and fills[0]["realizedPnL"] == pytest.approx(39)
    pf = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()
    assert pf["cash"] == pytest.approx(10000 - 502 + 440 - 1)  # portfolio cash follows every fill and fee


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
    monkeypatch.setenv("ALLOW_LIVE", "1")
    monkeypatch.setenv("ALPACA_LIVE_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_SECRET", "s")
    assert make_broker({"environment": "LIVE", "provider": "ALPACA", "credentialsRef": "ALPACA_LIVE"}).base == "https://api.alpaca.markets"
    monkeypatch.delenv("ALLOW_LIVE")
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(BrokerError, match="NOPE_KEY"):
        make_broker({"environment": "PAPER", "provider": "ALPACA", "credentialsRef": "NOPE"})


# ------------------------------------------------------------------ deleting budgets and automations


def test_deleting_a_budget_is_guarded(client, portfolio, automation, budget, broker):
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    order = buy(client, portfolio, quantity=5, budgetId=bid).json()  # open order
    r = client.delete(f"/v1/budgets/{bid}", headers=KEY)
    assert r.status_code == 409 and "open order" in r.text
    broker.fill(order["brokerOrderId"], 5, 10)
    sync(client, portfolio)
    r = client.delete(f"/v1/budgets/{bid}", headers=KEY)
    assert r.status_code == 409 and "holds shares (5 AAA)" in r.text

    sell = client.post(f"/v1/portfolios/{portfolio['id']}/orders", headers=KEY, json={
        "symbol": "AAA", "side": "SELL", "quantity": 5, "budgetId": bid}).json()
    broker.fill(sell["brokerOrderId"], 5, 11)
    sync(client, portfolio)
    assert client.delete(f"/v1/budgets/{bid}", headers=KEY).status_code == 204
    pf = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()
    assert pf["budgets"] == [] and pf["unallocatedCash"] == pytest.approx(10005)  # 10000 + 5 profit
    assert ledger(client, budget)[-1] == ("WITHDRAW", pytest.approx(-1005))
    assert client.get(f"/v1/automations/{automation['id']}", headers=KEY).json()["budgets"] == []
    events = [e["type"] for e in client.get(f"/v1/automations/{automation['id']}/events", headers=KEY).json()["events"]]
    assert events[0] == "BUDGET_REMOVED"


def test_deleting_an_automation_keeps_the_budget(client, portfolio, automation, budget, broker):
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    order = buy(client, portfolio, quantity=5, budgetId=bid).json()
    assert client.delete(f"/v1/automations/{automation['id']}", headers=KEY).status_code == 204
    b = client.get(f"/v1/budgets/{bid}", headers=KEY).json()
    assert b["automations"] == [] and b["cash"] == 1000 and b["reserved"] == pytest.approx(50.5)
    broker.fill(order["brokerOrderId"], 5, 10)  # the open order still settles into the budget
    sync(client, portfolio)
    b = client.get(f"/v1/budgets/{bid}", headers=KEY).json()
    assert b["invested"] == 50 and b["reserved"] == 0 and b["cash"] == 950


# ------------------------------------------------------------------ account fees (not part of a fill)


def today_et() -> str:
    from zoneinfo import ZoneInfo
    from datetime import datetime as dt
    return dt.now(ZoneInfo("America/New_York")).date().isoformat()


def test_account_fee_is_charged_to_whoever_traded(client, portfolio, automation, budget, broker):
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    for kw in ({"budgetId": bid}, {"budgetId": bid}, {}):  # 2 budget trades, 1 manual trade
        o = buy(client, portfolio, quantity=1, **kw).json()
        broker.fill(o["brokerOrderId"], 1, 10)
    broker.charge("0.04", today_et(), "CAT fee for proceed of 3 trades")
    out = sync(client, portfolio)
    assert out["sync"]["mismatches"] == []  # the app now matches the broker's cash to the cent
    assert out["portfolio"]["cash"] == pytest.approx(10000 - 30 - 0.04)
    b = client.get(f"/v1/budgets/{bid}", headers=KEY).json()
    assert b["fees"] == pytest.approx(0.03) and b["cash"] == pytest.approx(1000 - 20 - 0.03)  # 2/3 of 4 cents, rounded up
    entries = client.get(f"/v1/portfolios/{portfolio['id']}/ledger", headers=KEY).json()["entries"]
    fees = sorted((e["amount"], e["budgetId"]) for e in entries if e["type"] == "FEE")
    assert fees == [(-0.03, bid), (-0.01, None)] and "CAT fee" in entries[0]["note"]
    sync(client, portfolio)  # imported once
    assert client.get(f"/v1/budgets/{bid}", headers=KEY).json()["fees"] == pytest.approx(0.03)


def test_account_fee_on_a_day_without_app_trades(client, portfolio, broker):
    acct = portfolio["brokerAccountId"]
    client.post(f"/v1/portfolios/{portfolio['id']}/capital/withdraw", headers=KEY, json={"amount": 2500})
    other = client.post("/v1/portfolios", headers=KEY, json={"name": "Other", "brokerAccountId": acct, "capital": 2500}).json()
    broker.charge("0.05", today_et())
    sync(client, portfolio)
    cash = {p["id"]: p["cash"] for p in client.get("/v1/portfolios", headers=KEY).json()["portfolios"]}
    assert cash[portfolio["id"]] == pytest.approx(7500 - 0.04) and cash[other["id"]] == pytest.approx(2500 - 0.01)


def test_fees_from_before_the_account_was_added_are_ignored(client, portfolio, broker):
    broker.charge("1.00", "2020-01-02", "old fee")
    assert sync(client, portfolio)["portfolio"]["cash"] == 10000


# ------------------------------------------------------------------ dividends


def tomorrow_et() -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(today_et()) + timedelta(days=1)).isoformat()


def test_dividend_goes_to_whoever_held_the_shares(client, portfolio, automation, budget, broker):
    bid, pid = budget["id"], portfolio["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    for q, kw in ((3, {"budgetId": bid}), (1, {})):  # the budget holds 3 shares, the manual shares 1
        o = buy(client, portfolio, quantity=q, **kw).json()
        broker.fill(o["brokerOrderId"], q, 10)
    broker.pay_dividend("AAA", "0.40", tomorrow_et(), quantity=4)
    broker.pay_dividend("AAA", "-0.12", tomorrow_et(), kind="DIVNRA", quantity=4)  # tax withheld
    out = sync(client, portfolio)
    assert out["sync"]["mismatches"] == [] and out["portfolio"]["cash"] == pytest.approx(10000 - 40 + 0.28)
    assert [e for e in ledger(client, budget) if e[0] == "DIVIDEND"] == [("DIVIDEND", 0.3), ("DIVIDEND", -0.09)]
    sync(client, portfolio)  # imported once
    rep = client.get(f"/v1/portfolios/{pid}/dividends", headers=KEY, params={"year": tomorrow_et()[:4]}).json()
    assert rep["symbols"] == [{"symbol": "AAA", "received": 0.4, "withheld": -0.12, "net": 0.28}]
    assert rep["net"] == 0.28


def test_dividend_after_the_shares_were_sold_and_outside_shares(client, portfolio, automation, budget, broker):
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    o = buy(client, portfolio, quantity=2, budgetId=bid).json()
    broker.fill(o["brokerOrderId"], 2, 10)
    sync(client, portfolio)
    o = buy(client, portfolio, side="SELL", quantity=2, budgetId=bid).json()
    broker.fill(o["brokerOrderId"], 2, 10)
    broker.pay_dividend("AAA", "1.00", tomorrow_et(), quantity=4)  # sold before the pay date; 2 shares not the app's
    out = sync(client, portfolio)
    assert [e for e in ledger(client, budget) if e[0] == "DIVIDEND"] == [("DIVIDEND", 0.5)]
    assert out["portfolio"]["cash"] == pytest.approx(10000.5)  # the other half stays unassigned


def test_alpaca_dividend_activities():
    rec = Recorder([(200, [
        {"id": "d1", "activity_type": "DIV", "date": "2026-11-02", "symbol": "AAA", "qty": "4", "net_amount": "0.4",
         "per_share_amount": "0.1"},
        {"id": "d2", "activity_type": "DIVNRA", "date": "2026-11-02", "symbol": "AAA", "qty": "4",
         "net_amount": "-0.12"}])])
    divs = AlpacaBroker("k", "s", session=rec).list_dividends(None)
    assert [(d.broker_dividend_id, d.kind, str(d.amount), d.quantity) for d in divs] == [
        ("d1", "DIV", "0.4", 4), ("d2", "DIVNRA", "-0.12", 4)]
    assert "DIVNRA" in rec.calls[0][2]["activity_types"]


# ------------------------------------------------------------------ holdings no longer halal


def test_holdings_no_longer_halal_are_sold(client, portfolio, automation, budget, broker, market, settings, monkeypatch):
    from datetime import datetime, timedelta
    import fake_market
    from stocksvest_app import db
    from stocksvest_app.deps import AppContext
    from stocksvest_app.orders import halal_exit
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    for q, kw in ((3, {"budgetId": bid}), (2, {})):
        o = buy(client, portfolio, quantity=q, **kw).json()
        broker.fill(o["brokerOrderId"], q, 10)
    sync(client, portfolio)
    ctx, conn = AppContext(settings, market, broker_factory=lambda a: broker), db.connect(settings.db_path)
    opens = datetime.fromisoformat(market.calendar()["days"][0]["regularOpen"])

    market.removed = {"AAA": "PENDING"}  # a screening glitch: reported, never sold
    assert [(h["symbol"], h["budgetId"], h["quantity"]) for h in halal_exit.held_removed(ctx, conn)] == [
        ("AAA", None, 2), ("AAA", bid, 3)]
    assert halal_exit.run(ctx, conn, opens + timedelta(minutes=1)) == []

    market.removed = {"AAA": "NON_COMPLIANT"}
    monkeypatch.setattr("stocksvest_app.market.data.UNIVERSE_TTL_SECONDS", 0)
    monkeypatch.delitem(fake_market.UNIVERSE, "AAA")  # gone from the universe: no buys, but held shares can be sold
    assert buy(client, portfolio, quantity=1).status_code == 422
    assert buy(client, portfolio, side="SELL", quantity=1).status_code == 201
    assert halal_exit.run(ctx, conn, opens - timedelta(minutes=1)) == []  # only in the regular session
    orders = halal_exit.run(ctx, conn, opens + timedelta(minutes=1))
    assert sorted((o["budgetId"] or "", o["quantity"], o["type"]) for o in orders) == [
        ("", 1, "MARKET"), (bid, 3, "MARKET")]  # the manual share already being sold is left alone
    assert halal_exit.run(ctx, conn, opens + timedelta(minutes=2)) == []  # open sells cover everything
    conn.close()


def test_kill_all_stops_automations_cancels_buys_and_sells_everything(client, portfolio, automation, budget, broker):
    bid = budget["id"]
    client.post(f"/v1/budgets/{bid}/allocate", headers=KEY, json={"amount": 1000})
    for q, kw in ((3, {"budgetId": bid}), (2, {})):
        o = buy(client, portfolio, quantity=q, **kw).json()
        broker.fill(o["brokerOrderId"], q, 10)
    sync(client, portfolio)
    pending_sell = buy(client, portfolio, side="SELL", quantity=1).json()  # already being sold: not sold twice
    open_buy = buy(client, portfolio, quantity=1, budgetId=bid).json()
    assert client.post("/v1/orders/kill-all", headers=KEY, json={"confirm": "yes"}).status_code == 422
    out = client.post("/v1/orders/kill-all", headers=KEY, json={"confirm": "SELL ALL"}).json()
    assert out["cancelled"] == [open_buy["id"]] and out["errors"] == []
    assert sorted((s["budgetId"] or "", s["quantity"]) for s in out["sells"]) == [("", 1), (bid, 3)]
    assert client.get("/v1/automations/switch", headers=KEY).json()["automationsEnabled"] is False
    assert client.get(f"/v1/orders/{pending_sell['id']}", headers=KEY).json()["status"] != "CANCELLED"


def test_split_cents():
    from decimal import Decimal
    from stocksvest_app.orders.service import split_cents
    shares = split_cents(Decimal("0.10"), {"a": 1, "b": 1, "c": 1})
    assert sorted(shares.values()) == [Decimal("0.03"), Decimal("0.03"), Decimal("0.04")]
    assert sum(split_cents(Decimal("-0.07"), {"a": 2, "b": 1}).values()) == Decimal("-0.07")
    assert split_cents(Decimal("0.01"), {"a": 1, "b": 9}) == {"a": 0, "b": Decimal("0.01")}


def test_alpaca_fee_activities():
    rec = Recorder([(200, [{"id": "fee1", "activity_type": "FEE", "date": "2026-10-19", "net_amount": "-0.01",
                            "description": "CAT fee for proceed of 8 trades on 2026-10-19"}])])
    fees = AlpacaBroker("k", "s", session=rec).list_fees(None)
    assert [(f.broker_fee_id, str(f.amount), f.date) for f in fees] == [("fee1", "0.01", "2026-10-19")]
    assert rec.calls[0][1].endswith("/v2/account/activities") and rec.calls[0][2]["activity_types"] == "FEE,PTC"



# ------------------------------------------------------------------ live prices for manual orders and positions


def test_manual_orders_and_positions_use_live_prices(client, portfolio, broker, market):
    market.live = {"AAA": {"bid": 10.4, "ask": 10.6, "last": 10.5, "price": 10.5}}
    order = buy(client, portfolio, quantity=2).json()  # (positions are valued at the last trade, below)
    assert order["estimatedPrice"] == 10.6  # a live buy is estimated at the ask (delayed price: 10)
    broker.fill(order["brokerOrderId"], 2, "10.6")
    sync(client, portfolio)
    pos = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()["positions"][0]
    assert (pos["price"], pos["priceSource"], pos["marketValue"]) == (10.5, "IBKR", 21.0)
    assert sync(client, portfolio)["portfolio"]["positions"][0]["price"] == 10.5  # "Sync now" shows the same prices
    market.live = {"AAA": {"bid": 10.0, "ask": 11.4, "last": 10.3, "price": 10.7}}  # wide after-hours quote
    pos = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()["positions"][0]
    assert pos["price"] == 10.3  # the last trade, like the broker shows it, not the 10.7 midpoint
    market.live = {"AAA": {"bid": 6.0, "ask": 10.2, "last": None, "price": 8.1}}  # no trade yet before the open
    pos = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()["positions"][0]
    assert (pos["price"], pos["priceSource"]) == (10.0, "DELAYED")  # the delayed last trade, not the midpoint
    market.live = {}
    pos = client.get(f"/v1/portfolios/{portfolio['id']}", headers=KEY).json()["positions"][0]
    assert (pos["price"], pos["priceSource"]) == (10.0, "DELAYED")
