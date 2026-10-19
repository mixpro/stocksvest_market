from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.automations import runner
from stocksvest_app.brokers.base import BrokerError
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


BROKEN: set[str] = set()  # broker account names whose broker can't be reached


def broker_for(account, broker):
    if account["name"] in BROKEN:
        raise BrokerError("Alpaca unreachable: connection refused", retryable=True)
    return broker


@pytest.fixture
def client(tmp_path, monkeypatch, broker):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    BROKEN.clear()
    market = FakeMarket(last_sync=datetime.now(timezone.utc).isoformat())
    with TestClient(create_app(Settings(), market_factory=lambda s: market,
                               broker_factory=lambda a: broker_for(a, broker))) as c:
        yield c


@pytest.fixture
def setup(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    pf = client.post("/v1/portfolios", headers=KEY,
                     json={"name": "Paper", "brokerAccountId": acct["id"], "capital": 10000}).json()
    everything = client.post("/v1/filters", headers=KEY, json={"name": "All", "inputs": [{"type": "UNIVERSE"}]}).json()
    return {"portfolio": pf, "filter": everything}


def order_action(side="BUY", sizings=None, **kw):
    return {"type": "PLACE_ORDER", "side": side, "sizings": sizings or [{"mode": "CASH_AMOUNT", "value": 100}], **kw}


def automation(client, setup, action, guards=None, schedules=None, name="Bot"):
    body = {"name": name, "runTargets": [{"type": "FILTER", "id": setup["filter"]["id"]}],
            "schedules": schedules or [{"type": "DAILY", "atTimes": ["10:00"]}],
            "actions": [action], "orderGuards": {"skipBuyIfHeld": True, **(guards or {})}}
    r = client.post("/v1/automations", headers=KEY, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def new_budget(client, portfolio, amount, *automations, name=None):
    r = client.post(f"/v1/portfolios/{portfolio['id']}/budgets", headers=KEY,
                    json={"name": name or f"B{len(client.get(f'/v1/portfolios/{portfolio['id']}/budgets', headers=KEY).json()['budgets'])}",
                          "amount": amount, "automationIds": [a["id"] for a in automations]})
    assert r.status_code == 201, r.text
    return r.json()


def make(client, setup, action, guards=None, budget=1000, schedules=None):
    """An automation in a budget of its own (budget=None: in no budget)."""
    a = automation(client, setup, action, guards, schedules)
    if budget is not None:
        a["budget"] = new_budget(client, setup["portfolio"], budget, a)
    return a


def run(client, a, preview=False):
    return client.post(f"/v1/automations/{a['id']}/{'preview' if preview else 'run'}", headers=KEY).json()


def section(r, budget_index=0, action_index=0) -> dict:
    """One budget's part of an order action's result."""
    return r["result"]["actions"][action_index]["budgets"][budget_index]


def intents(r, budget_index=0) -> list[tuple]:
    return [(i["ticker"], i["quantity"], i["decision"], i["skipReason"]) for i in section(r, budget_index)["intents"]]


def test_buy_all_matches_with_cash_amount(client, setup, broker):
    a = make(client, setup, order_action())
    r = run(client, a)
    assert r["status"] == "SUCCEEDED"
    assert intents(r) == [("AAA", 10, "PLACE", None), ("BBB", 2, "PLACE", None), ("CCC", 20, "PLACE", None),
                          ("DDD", 0, "SKIP", "NO_PRICE")]
    res = r["result"]
    assert res["metrics"]["placedCount"] == 3 and res["metrics"]["estimatedBuyCost"] == 303
    b = res["budgets"][0]
    assert b["before"]["available"] == 1000 and b["after"]["reserved"] == 303 and b["budgetId"] == a["budget"]["id"]
    assert b["environment"] == "PAPER" and b["portfolioId"] == setup["portfolio"]["id"]
    bid = a["budget"]["id"]
    assert [x.client_order_id for x in broker.submitted] == [f"{r['id']}-0-{bid}-{t}" for t in ("AAA", "BBB", "CCC")]
    orders = client.get(f"/v1/portfolios/{setup['portfolio']['id']}/orders?automationId={a['id']}", headers=KEY).json()
    assert {o["automationRunId"] for o in orders["orders"]} == {r["id"]}
    assert {o["budgetId"] for o in orders["orders"]} == {bid}


def test_top_n_with_a_sizing_per_rank(client, setup):
    action = order_action(selection={"mode": "TOP_N", "topN": 2, "rankBy": "DAILY_CHANGE_PERCENT"},
                          sizings=[{"mode": "QUANTITY", "value": 5}, {"mode": "QUANTITY", "value": 1}])
    r = run(client, make(client, setup, action))
    assert [(i["ticker"], i["rank"], i["quantity"]) for i in section(r)["intents"]] == [
        ("AAA", 1, 5), ("CCC", 2, 1)]  # +11.1%, 0.0% (BBB -16.7% is third)


def test_budget_is_spent_top_rank_first(client, setup):
    action = order_action(selection={"mode": "TOP_N", "topN": 3, "rankBy": "LAST_PRICE"})  # BBB 50, AAA 10, CCC 5
    r = run(client, make(client, setup, action, budget=210))
    assert intents(r) == [("BBB", 2, "PLACE", None), ("AAA", 10, "PLACE", None), ("CCC", 20, "SKIP", "INSUFFICIENT_BUDGET")]


def test_guards(client, setup, broker):
    a = make(client, setup, order_action(), guards={"maxOrdersPerRun": 1})
    assert [x[3] for x in intents(run(client, a))] == [None, "MAX_ORDERS_REACHED", "MAX_ORDERS_REACHED", "NO_PRICE"]

    capped = make(client, setup, order_action(), guards={"maxPositionValue": 30}, budget=500)
    first = section(run(client, capped))["intents"][0]
    assert first["quantity"] == 3 and "reduced from 10 to 3" in first["detail"]

    MARKET.update(dataAgeMinutes=45)
    assert {x[3] for x in intents(run(client, capped, preview=True))} == {"STALE_DATA"}
    MARKET.update(dataAgeMinutes=16, session="CLOSED")
    assert {x[3] for x in intents(run(client, capped, preview=True))} == {"OUTSIDE_REGULAR_HOURS"}
    no_hours_limit = make(client, setup, order_action(), guards={"regularHoursOnly": False}, budget=200)
    assert intents(run(client, no_hours_limit, preview=True))[0][2] == "PLACE"


def fill_all(broker):
    for o in broker.orders.values():
        if o.filled_quantity < o.quantity:
            broker.fill(o.broker_order_id, o.quantity - o.filled_quantity,
                        10 if o.symbol == "AAA" else 50 if o.symbol == "BBB" else 5)


def test_a_buyer_and_a_seller_share_one_budget(client, setup, broker):
    buyer = automation(client, setup, order_action(sizings=[{"mode": "QUANTITY", "value": 4}]), name="Buyer")
    seller = automation(client, setup, order_action("SELL", [{"mode": "PERCENT_OF_POSITION", "value": 50}]),
                        name="Seller")
    shared = new_budget(client, setup["portfolio"], 1000, buyer, seller, name="Shared")
    other = new_budget(client, setup["portfolio"], 100, name="Other")

    first = run(client, buyer)
    fill_all(broker)
    again = run(client, buyer)  # syncs fills first: the budget now holds AAA, BBB, CCC
    assert [x[3] for x in intents(again)][:3] == ["ALREADY_HELD"] * 3
    assert first["result"]["budgets"][0]["before"]["invested"] == 0
    assert again["result"]["budgets"][0]["before"]["invested"] == 260

    r = run(client, seller)  # sells the shares the buyer bought with the same budget
    assert intents(r) == [("AAA", 2, "PLACE", None), ("BBB", 2, "PLACE", None), ("CCC", 2, "PLACE", None)]
    assert section(r)["notHeld"] == 1 and r["result"]["metrics"]["estimatedSellProceeds"] == 130
    fill_all(broker)
    client.post(f"/v1/portfolios/{setup['portfolio']['id']}/sync", headers=KEY)
    money = client.get(f"/v1/budgets/{shared['id']}", headers=KEY).json()
    assert money["cash"] == 1000 - 260 + 130 and money["invested"] == 130  # the sale went back into the shared budget
    entries = client.get(f"/v1/budgets/{shared['id']}/ledger", headers=KEY).json()["entries"]
    assert {e["automationId"] for e in entries if e["type"] == "SELL_FILL"} == {seller["id"]}

    # A seller in another budget can't touch them.
    client.delete(f"/v1/budgets/{shared['id']}/automations/{seller['id']}", headers=KEY)
    client.post(f"/v1/budgets/{other['id']}/automations", headers=KEY, json={"automationId": seller["id"]})
    r = run(client, seller)
    assert r["result"]["budgets"][0]["budgetId"] == other["id"]
    assert intents(r) == [] and section(r)["notHeld"] == 4

    record = client.get(f"/v1/automations/{seller['id']}/record", headers=KEY).json()
    assert record["total"]["sellFills"] == 3 and record["total"]["received"] == 130
    assert record["total"]["realizedPnL"] == 0 and record["budgets"][0]["name"] == "Shared"
    buys = client.get(f"/v1/automations/{buyer['id']}/record", headers=KEY).json()["total"]
    assert buys["buyFills"] == 3 and buys["spent"] == 260 and buys["sellFills"] == 0


def test_one_automation_in_budgets_of_two_portfolios(client, setup, broker):
    acct = client.get("/v1/broker-accounts", headers=KEY).json()["accounts"][0]
    second = client.post("/v1/portfolios", headers=KEY, json={"name": "Second", "brokerAccountId": acct["id"]}).json()
    client.post(f"/v1/portfolios/{setup['portfolio']['id']}/capital/withdraw", headers=KEY, json={"amount": 2000})
    client.post(f"/v1/portfolios/{second['id']}/capital/allocate", headers=KEY, json={"amount": 2000})
    a = automation(client, setup, order_action(selection={"mode": "TOP_N", "topN": 1, "rankBy": "LAST_PRICE"}))
    b1 = new_budget(client, setup["portfolio"], 500, a, name="Main")
    b2 = new_budget(client, second, 200, a, name="Small")
    got = client.get(f"/v1/automations/{a['id']}", headers=KEY).json()["budgets"]
    assert {b["id"] for b in got} == {b1["id"], b2["id"]}

    r = run(client, a)
    by_budget = {s["budgetId"]: s for s in r["result"]["actions"][0]["budgets"]}
    assert [(i["ticker"], i["decision"]) for i in by_budget[b1["id"]]["intents"]] == [("BBB", "PLACE")]
    assert [(i["ticker"], i["decision"], i["skipReason"]) for i in by_budget[b2["id"]]["intents"]] == [
        ("BBB", "PLACE", None)]
    assert len(broker.submitted) == 2  # the same stock bought once per budget, with distinct order ids
    assert len({x.client_order_id for x in broker.submitted}) == 2
    orders = {o["budgetId"]: o for p in (setup["portfolio"], second)
              for o in client.get(f"/v1/portfolios/{p['id']}/orders", headers=KEY).json()["orders"]}
    assert orders[b1["id"]]["portfolioId"] == setup["portfolio"]["id"] and orders[b2["id"]]["portfolioId"] == second["id"]
    assert r["result"]["metrics"]["budgetCount"] == 2


def test_a_failing_broker_skips_only_its_budget(client, setup, broker):
    other_acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Flaky", "provider": "ALPACA"}).json()
    other = client.post("/v1/portfolios", headers=KEY,
                        json={"name": "Elsewhere", "brokerAccountId": other_acct["id"], "capital": 0}).json()
    a = automation(client, setup, order_action(sizings=[{"mode": "QUANTITY", "value": 1}]))
    ok = new_budget(client, setup["portfolio"], 500, a, name="Works")
    new_budget(client, other, 0, a, name="Down")
    BROKEN.add("Flaky")
    r = run(client, a)
    assert r["status"] == "PARTIAL" and r["result"]["actions"][0]["status"] == "PARTIAL"
    parts = {s["name"]: s for s in r["result"]["actions"][0]["budgets"]}
    assert parts["Down"]["status"] == "FAILED" and "unreachable" in parts["Down"]["error"]
    assert parts["Works"]["status"] == "SUCCEEDED" and len(parts["Works"]["intents"]) == 4
    assert {o.symbol for o in broker.submitted} == {"AAA", "BBB", "CCC"}
    assert ok["id"] in {b["budgetId"] for b in r["result"]["budgets"] if "error" not in b}


def test_preview_places_nothing(client, setup, broker):
    a = make(client, setup, order_action())
    p = run(client, a, preview=True)
    assert p["preview"] is True and [x[2] for x in intents(p)] == ["PLACE", "PLACE", "PLACE", "SKIP"]
    assert broker.submitted == [] and client.get(f"/v1/budgets/{a['budget']['id']}", headers=KEY).json()["reserved"] == 0


def test_limit_orders_get_prices_from_offsets(client, setup, broker):
    a = make(client, setup, order_action(orderType="LIMIT", limitOffsetPercent=-1,
                                         sizings=[{"mode": "QUANTITY", "value": 10}]))
    first = section(run(client, a))["intents"][0]
    assert first["limitPrice"] == 9.9 and first["estimatedCost"] == 99
    assert float(broker.submitted[0].limit_price) == pytest.approx(9.9)


def test_broker_rejection_marks_intent_failed(client, setup, broker):
    a = make(client, setup, order_action(selection={"mode": "TOP_N", "topN": 2, "rankBy": "LAST_PRICE"}))
    broker.fail_next = BrokerError("Alpaca refused (403): account restricted")
    r = run(client, a)
    assert r["status"] == "PARTIAL" and r["result"]["actions"][0]["status"] == "PARTIAL"
    assert [(i["decision"], i["orderStatus"]) for i in section(r)["intents"]] == [
        ("FAILED", "REJECTED"), ("PLACE", "SUBMITTED")]
    budget = client.get(f"/v1/budgets/{a['budget']['id']}", headers=KEY).json()
    assert budget["reserved"] == 101  # the rejected order's reservation was given back


def test_not_in_any_budget_skips_order_actions(client, setup, broker):
    a = make(client, setup, order_action(), budget=None)
    r = run(client, a)
    action = r["result"]["actions"][0]
    assert r["status"] == "SUCCEEDED" and action["status"] == "SKIPPED" and "not in any budget" in action["detail"]
    assert broker.submitted == [] and r["result"]["matched"]


def test_validation(client, setup):
    base = {"name": "x", "runTargets": [{"type": "FILTER", "id": setup["filter"]["id"]}],
            "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}]}

    def err(action, **extra):
        return client.post("/v1/automations", headers=KEY, json={**base, "actions": [action], **extra}).text

    assert "RUN_ALL is not allowed" in err(order_action(), schedules=[{"type": "DAILY", "atTimes": ["10:00"], "missedPolicy": "RUN_ALL"}])
    assert "can't be sized by CASH_AMOUNT" in err(order_action("SELL"))
    assert "exactly one sizing" in err(order_action(sizings=[{"mode": "QUANTITY", "value": 1}] * 2))
    assert "needs limitOffsetPercent" in err(order_action(orderType="LIMIT"))
    assert "TOP_N needs topN and rankBy" in err(order_action(selection={"mode": "TOP_N"}))
    created = client.post("/v1/automations", headers=KEY, json={**base, "actions": [order_action()]}).json()
    assert created["orderGuards"]["maxOrdersPerRun"] == 10 and created["orderGuards"]["regularHoursOnly"] is True
    assert created["budgets"] == [] and "portfolioId" not in created
