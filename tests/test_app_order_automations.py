from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.automations import runner
from stocksvest_app.config import Settings

KEY = {"X-API-Key": "app-key"}
MARKET = {"session": "REGULAR", "dataAgeMinutes": 16}  # what market_context reports, changed per test


@pytest.fixture(autouse=True)
def market_now(monkeypatch):
    MARKET.update(session="REGULAR", dataAgeMinutes=16)
    monkeypatch.setattr(runner, "market_context", lambda ctx, now: dict(MARKET, marketLastSyncAt=None))


@pytest.fixture
def broker():
    return FakeBroker(cash="10000")


@pytest.fixture
def client(tmp_path, monkeypatch, broker):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    market = FakeMarket(last_sync=datetime.now(timezone.utc).isoformat())
    with TestClient(create_app(Settings(), market_factory=lambda s: market, broker_factory=lambda a: broker)) as c:
        yield c


@pytest.fixture
def setup(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    pf = client.post("/v1/portfolios", headers=KEY, json={"name": "Paper", "brokerAccountId": acct["id"]}).json()
    everything = client.post("/v1/filters", headers=KEY, json={"name": "All", "inputs": [{"type": "UNIVERSE"}]}).json()
    return {"portfolio": pf, "filter": everything}


def order_action(side="BUY", sizings=None, **kw):
    return {"type": "PLACE_ORDER", "side": side, "sizings": sizings or [{"mode": "CASH_AMOUNT", "value": 100}], **kw}


def make(client, setup, action, guards=None, budget=1000, schedules=None):
    body = {"name": "Bot", "portfolioId": setup["portfolio"]["id"],
            "runTargets": [{"type": "FILTER", "id": setup["filter"]["id"]}],
            "schedules": schedules or [{"type": "DAILY", "atTimes": ["10:00"]}],
            "actions": [action], "orderGuards": {"skipBuyIfHeld": True, **(guards or {})}}
    r = client.post("/v1/automations", headers=KEY, json=body)
    assert r.status_code == 201, r.text
    a = r.json()
    if budget:
        client.post(f"/v1/automations/{a['id']}/budget/allocate", headers=KEY, json={"amount": budget})
    return a


def run(client, a, preview=False):
    return client.post(f"/v1/automations/{a['id']}/{'preview' if preview else 'run'}", headers=KEY).json()


def intents(r) -> list[tuple]:
    res = r["result"]
    return [(i["ticker"], i["quantity"], i["decision"], i["skipReason"]) for i in res["actions"][0]["intents"]]


def test_buy_all_matches_with_cash_amount(client, setup, broker):
    a = make(client, setup, order_action())
    r = run(client, a)
    assert r["status"] == "SUCCEEDED"
    assert intents(r) == [("AAA", 10, "PLACE", None), ("BBB", 2, "PLACE", None), ("CCC", 20, "PLACE", None),
                          ("DDD", 0, "SKIP", "NO_PRICE")]
    res = r["result"]
    assert res["metrics"]["placedCount"] == 3 and res["metrics"]["estimatedBuyCost"] == 303
    assert res["budgetBefore"]["available"] == 1000 and res["budgetAfter"]["reserved"] == 303
    assert res["environment"] == "PAPER" and res["portfolioId"] == setup["portfolio"]["id"]
    assert [x.client_order_id for x in broker.submitted] == [f"{r['id']}-0-{t}" for t in ("AAA", "BBB", "CCC")]
    orders = client.get(f"/v1/portfolios/{setup['portfolio']['id']}/orders?automationId={a['id']}", headers=KEY).json()
    assert {o["automationRunId"] for o in orders["orders"]} == {r["id"]}


def test_top_n_with_a_sizing_per_rank(client, setup):
    action = order_action(selection={"mode": "TOP_N", "topN": 2, "rankBy": "DAILY_CHANGE_PERCENT"},
                          sizings=[{"mode": "QUANTITY", "value": 5}, {"mode": "QUANTITY", "value": 1}])
    r = run(client, make(client, setup, action))
    assert [(i["ticker"], i["rank"], i["quantity"]) for i in r["result"]["actions"][0]["intents"]] == [
        ("AAA", 1, 5), ("CCC", 2, 1)]  # +11.1%, 0.0% (BBB -16.7% is third)


def test_budget_is_spent_top_rank_first(client, setup):
    action = order_action(selection={"mode": "TOP_N", "topN": 3, "rankBy": "LAST_PRICE"})  # BBB 50, AAA 10, CCC 5
    r = run(client, make(client, setup, action, budget=210))
    assert intents(r) == [("BBB", 2, "PLACE", None), ("AAA", 10, "PLACE", None), ("CCC", 20, "SKIP", "INSUFFICIENT_BUDGET")]


def test_guards(client, setup, broker):
    a = make(client, setup, order_action(), guards={"maxOrdersPerRun": 1})
    assert [x[3] for x in intents(run(client, a))] == [None, "MAX_ORDERS_REACHED", "MAX_ORDERS_REACHED", "NO_PRICE"]

    capped = make(client, setup, order_action(), guards={"maxPositionValue": 30}, budget=500)
    first = run(client, capped)["result"]["actions"][0]["intents"][0]
    assert first["quantity"] == 3 and "reduced from 10 to 3" in first["detail"]

    MARKET.update(dataAgeMinutes=45)
    assert {x[3] for x in intents(run(client, capped, preview=True))} == {"STALE_DATA"}
    MARKET.update(dataAgeMinutes=16, session="CLOSED")
    assert {x[3] for x in intents(run(client, capped, preview=True))} == {"OUTSIDE_REGULAR_HOURS"}
    no_hours_limit = make(client, setup, order_action(), guards={"regularHoursOnly": False}, budget=200)
    assert intents(run(client, no_hours_limit, preview=True))[0][2] == "PLACE"


def test_already_held_and_sell_own_shares(client, setup, broker):
    buyer = make(client, setup, order_action(sizings=[{"mode": "QUANTITY", "value": 4}]))
    first = run(client, buyer)
    for o in broker.orders.values():
        broker.fill(o.broker_order_id, o.quantity, 10 if o.symbol == "AAA" else 50 if o.symbol == "BBB" else 5)
    again = run(client, buyer)  # syncs fills first: now it holds AAA, BBB, CCC
    assert [x[3] for x in intents(again)][:3] == ["ALREADY_HELD"] * 3
    assert first["result"]["budgetBefore"]["invested"] == 0 and again["result"]["budgetBefore"]["invested"] == 260

    seller = make(client, setup, order_action("SELL", [{"mode": "PERCENT_OF_POSITION", "value": 50}]), budget=10)
    r = run(client, seller)
    assert intents(r) == [] and r["result"]["actions"][0]["notHeld"] == 4  # it owns nothing: the buyer's shares aren't its

    sell_own = client.put(f"/v1/automations/{buyer['id']}", headers=KEY, json={
        "name": "Bot", "portfolioId": setup["portfolio"]["id"], "runTargets": [{"type": "FILTER", "id": setup["filter"]["id"]}],
        "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}],
        "actions": [order_action("SELL", [{"mode": "PERCENT_OF_POSITION", "value": 50}])]}).json()
    r = run(client, sell_own)
    assert intents(r) == [("AAA", 2, "PLACE", None), ("BBB", 2, "PLACE", None), ("CCC", 2, "PLACE", None)]
    assert r["result"]["metrics"]["estimatedSellProceeds"] == 130


def test_preview_places_nothing(client, setup, broker):
    a = make(client, setup, order_action())
    p = run(client, a, preview=True)
    assert p["preview"] is True and [x[2] for x in intents(p)] == ["PLACE", "PLACE", "PLACE", "SKIP"]
    assert broker.submitted == [] and client.get(f"/v1/automations/{a['id']}/budget", headers=KEY).json()["pool"]["reserved"] == 0


def test_limit_orders_get_prices_from_offsets(client, setup, broker):
    a = make(client, setup, order_action(orderType="LIMIT", limitOffsetPercent=-1,
                                         sizings=[{"mode": "QUANTITY", "value": 10}]))
    first = run(client, a)["result"]["actions"][0]["intents"][0]
    assert first["limitPrice"] == 9.9 and first["estimatedCost"] == 99
    assert float(broker.submitted[0].limit_price) == pytest.approx(9.9)


def test_broker_rejection_marks_intent_failed(client, setup, broker):
    from stocksvest_app.brokers.base import BrokerError
    a = make(client, setup, order_action(selection={"mode": "TOP_N", "topN": 2, "rankBy": "LAST_PRICE"}))
    broker.fail_next = BrokerError("Alpaca refused (403): account restricted")
    r = run(client, a)
    assert r["status"] == "PARTIAL" and r["result"]["actions"][0]["status"] == "PARTIAL"
    assert [(i["decision"], i["orderStatus"]) for i in r["result"]["actions"][0]["intents"]] == [
        ("FAILED", "REJECTED"), ("PLACE", "SUBMITTED")]
    pool = client.get(f"/v1/automations/{a['id']}/budget", headers=KEY).json()["pool"]
    assert pool["reserved"] == 101  # the rejected order's reservation was given back


def test_no_budget_fails_the_action(client, setup):
    a = make(client, setup, order_action(), budget=0)
    r = run(client, a)
    assert r["status"] == "FAILED" and "no budget" in r["result"]["actions"][0]["error"]


def test_validation(client, setup):
    base = {"name": "x", "runTargets": [{"type": "FILTER", "id": setup["filter"]["id"]}],
            "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}]}

    def err(action, **extra):
        return client.post("/v1/automations", headers=KEY,
                           json={**base, "portfolioId": setup["portfolio"]["id"], "actions": [action], **extra}).text

    assert "need a portfolioId" in client.post("/v1/automations", headers=KEY, json={**base, "actions": [order_action()]}).text
    assert "RUN_ALL is not allowed" in err(order_action(), schedules=[{"type": "DAILY", "atTimes": ["10:00"], "missedPolicy": "RUN_ALL"}])
    assert "can't be sized by CASH_AMOUNT" in err(order_action("SELL"))
    assert "exactly one sizing" in err(order_action(sizings=[{"mode": "QUANTITY", "value": 1}] * 2))
    assert "needs limitOffsetPercent" in err(order_action(orderType="LIMIT"))
    assert "TOP_N needs topN and rankBy" in err(order_action(selection={"mode": "TOP_N"}))
    created = client.post("/v1/automations", headers=KEY, json={
        **base, "portfolioId": setup["portfolio"]["id"], "actions": [order_action()]}).json()
    assert created["orderGuards"]["maxOrdersPerRun"] == 10 and created["orderGuards"]["regularHoursOnly"] is True
