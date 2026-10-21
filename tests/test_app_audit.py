"""The activity log records every operation: API changes (success and failure) and what the app does itself."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app import db
from stocksvest_app.api import create_app
from stocksvest_app.audit.middleware import describe, fingerprint
from stocksvest_app.automations import scheduler, store as automations_store
from stocksvest_app.config import Settings
from stocksvest_app.deps import AppContext

KEY = {"X-API-Key": "app-key"}
ET = ZoneInfo("America/New_York")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    return Settings()


@pytest.fixture
def broker():
    return FakeBroker(cash="10000")


@pytest.fixture
def client(settings, broker):
    with TestClient(create_app(settings, market_factory=lambda s: FakeMarket(last_sync=datetime.now(timezone.utc).isoformat()),
                               broker_factory=lambda a: broker)) as c:
        yield c


def log(client, **filters) -> list[dict]:
    q = "&".join(f"{k}={v}" for k, v in filters.items())
    return client.get(f"/v1/activity?{q}", headers=KEY).json()["entries"]


def test_action_names():
    assert describe("POST", "/v1/filters") == ("FILTER_CREATE", "FILTER")
    assert describe("PUT", "/v1/filters/{filter_id}") == ("FILTER_UPDATE", "FILTER")
    assert describe("DELETE", "/v1/filter-groups/{group_id}") == ("FILTER_GROUP_DELETE", "FILTER_GROUP")
    assert describe("POST", "/v1/automations/{automation_id}/pause") == ("AUTOMATION_PAUSE", "AUTOMATION")
    assert describe("POST", "/v1/budgets/{budget_id}/allocate") == ("BUDGET_ALLOCATE", "BUDGET")
    assert describe("POST", "/v1/portfolios/{portfolio_id}/budgets") == ("BUDGET_CREATE", "BUDGET")
    assert describe("DELETE", "/v1/budgets/{budget_id}/automations/{automation_id}") == ("BUDGET_AUTOMATIONS", "BUDGET")
    assert describe("POST", "/v1/portfolios/{portfolio_id}/capital/withdraw") == ("PORTFOLIO_CAPITAL_WITHDRAW", "PORTFOLIO")
    assert describe("POST", "/v1/portfolios/{portfolio_id}/orders") == ("ORDER_PLACE_MANUAL", "PORTFOLIO")
    assert describe("POST", "/v1/filters/preview") == ("FILTER_PREVIEW", "FILTER")


def test_api_changes_are_logged_with_who_what_and_result(client):
    f = client.post("/v1/filters", headers=KEY, json={"name": "Tech", "inputs": [{"type": "UNIVERSE"}]})
    assert f.headers["x-correlation-id"].startswith("req_")
    fid = f.json()["id"]
    client.put(f"/v1/filters/{fid}", headers=KEY, json={"name": "Tech 2", "inputs": [{"type": "UNIVERSE"}]})
    client.get(f"/v1/filters/{fid}", headers=KEY)  # reads are not logged
    client.delete(f"/v1/filters/{fid}", headers=KEY)

    entries = log(client, entityId=fid)
    assert [e["action"] for e in entries] == ["FILTER_DELETE", "FILTER_UPDATE", "FILTER_CREATE"]
    created = entries[-1]
    assert created["source"] == "API" and created["actor"] == fingerprint("app-key") and "app-key" not in created["actor"]
    assert created["status"] == 201 and created["outcome"] == "OK" and created["details"]["request"]["name"] == "Tech"
    assert created["correlationId"] == f.headers["x-correlation-id"]


def test_failures_are_logged_too(client):
    bad = client.post("/v1/filters", headers=KEY, json={"name": "", "inputs": []})
    assert bad.status_code == 422
    unknown = client.delete("/v1/automations/aut_nope", headers=KEY)
    assert unknown.status_code == 404
    no_key = client.post("/v1/filters", json={"name": "x", "inputs": [{"type": "UNIVERSE"}]})
    assert no_key.status_code == 401

    failed = log(client, outcome="FAILED")
    assert [(e["action"], e["status"], e["actor"]) for e in failed] == [
        ("FILTER_CREATE", 401, "anonymous"), ("AUTOMATION_DELETE", 404, fingerprint("app-key")),
        ("FILTER_CREATE", 422, fingerprint("app-key"))]
    assert "not found" in failed[1]["error"] and failed[1]["entityId"] == "aut_nope"
    assert "name" in failed[2]["error"]


def test_related_ids_link_entries(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    pf = client.post("/v1/portfolios", headers=KEY, json={"name": "P", "brokerAccountId": acct["id"], "capital": 1000}).json()
    client.post(f"/v1/portfolios/{pf['id']}/capital/withdraw", headers=KEY, json={"amount": 100})
    about_account = [e["action"] for e in log(client, entityId=acct["id"])]
    assert about_account == ["PORTFOLIO_CREATE", "BROKER_ACCOUNT_CREATE"]  # the portfolio mentions the account
    assert [e["action"] for e in log(client, entityId=pf["id"])] == ["PORTFOLIO_CAPITAL_WITHDRAW", "PORTFOLIO_CREATE"]


def test_budget_actions_name_the_budget(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    pf = client.post("/v1/portfolios", headers=KEY, json={"name": "P", "brokerAccountId": acct["id"], "capital": 1000}).json()
    b = client.post(f"/v1/portfolios/{pf['id']}/budgets", headers=KEY, json={"name": "Main", "amount": 100}).json()
    client.post(f"/v1/budgets/{b['id']}/withdraw", headers=KEY, json={"amount": 40})
    entries = log(client, entityType="BUDGET")
    assert [(e["action"], e["entityId"]) for e in entries] == [("BUDGET_WITHDRAW", b["id"]), ("BUDGET_CREATE", b["id"])]
    assert entries[1]["related"]["portfolioId"] == pf["id"]


def test_orders_and_broker_events_share_the_request(client, broker):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    pf = client.post("/v1/portfolios", headers=KEY, json={"name": "P", "brokerAccountId": acct["id"], "capital": 1000}).json()
    r = client.post(f"/v1/portfolios/{pf['id']}/orders", headers=KEY, json={"symbol": "AAA", "side": "BUY", "quantity": 2})
    order = r.json()
    same = log(client, correlationId=r.headers["x-correlation-id"])
    assert [e["action"] for e in same] == ["ORDER_PLACE_MANUAL", "ORDER_PLACED"]
    placed = same[1]
    assert placed["entityId"] == order["id"] and placed["related"]["portfolioId"] == pf["id"]
    assert placed["details"]["symbol"] == "AAA" and placed["details"]["status"] == "SUBMITTED"

    broker.fill(order["brokerOrderId"], 2, 10)
    client.post(f"/v1/portfolios/{pf['id']}/sync", headers=KEY)
    order_log = [(e["action"], e["source"]) for e in log(client, entityId=order["id"])]
    assert order_log[:2] == [("ORDER_FILLED", "SYNC"), ("FILL_IMPORTED", "SYNC")]
    assert ("ORDER_PLACED", "API") in order_log


def test_broker_problems_are_logged(client, broker, settings):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    pf = client.post("/v1/portfolios", headers=KEY, json={"name": "P", "brokerAccountId": acct["id"], "capital": 1000}).json()
    from stocksvest_app.brokers.base import BrokerError
    broker.fail_next = BrokerError("Alpaca refused (403): account restricted")
    client.post(f"/v1/portfolios/{pf['id']}/orders", headers=KEY, json={"symbol": "AAA", "side": "BUY", "quantity": 1})
    rejected = log(client, action="ORDER_REJECTED")[0]
    assert rejected["outcome"] == "FAILED" and "restricted" in rejected["error"]

    original = broker.account_snapshot
    broker.account_snapshot = lambda: (_ for _ in ()).throw(BrokerError("Alpaca unreachable", retryable=True))
    assert client.post(f"/v1/portfolios/{pf['id']}/sync", headers=KEY).status_code == 502
    broker.account_snapshot = original
    failed = log(client, action="BROKER_SYNC_FAILED")[0]
    assert failed["source"] == "SYNC" and "unreachable" in failed["error"] and failed["entityId"] == acct["id"]


def test_scheduled_runs_are_logged(client, settings):
    f = client.post("/v1/filters", headers=KEY, json={"name": "All", "inputs": [{"type": "UNIVERSE"}]}).json()
    a = client.post("/v1/automations", headers=KEY, json={
        "name": "Daily", "runTargets": [{"type": "FILTER", "id": f["id"]}],
        "schedules": [{"type": "DAILY", "atTimes": ["10:00"], "missedPolicy": "SKIP"}],
        "actions": [{"type": "NOTIFY", "title": "x"}]}).json()
    ctx, conn = AppContext(settings, FakeMarket()), db.connect(settings.db_path)
    scheduler.tick(ctx, conn, datetime(2026, 10, 19, 9, 0, tzinfo=ET))
    scheduler.tick(ctx, conn, datetime(2026, 10, 19, 10, 0, 30, tzinfo=ET))
    scheduler.tick(ctx, conn, datetime(2026, 10, 22, 11, 0, tzinfo=ET))  # missed Tue/Wed/Thu
    entries = log(client, source="SCHEDULER")
    assert [e["action"] for e in entries] == ["AUTOMATION_RUNS_MISSED", "AUTOMATION_RUN"]
    run = entries[1]
    assert run["entityId"] == a["id"] and run["related"]["runId"].startswith("run_") and run["details"]["status"] == "SUCCEEDED"
    assert run["actor"] == "scheduler" and entries[0]["details"]["count"] == 3


def test_paging_and_action_list(client):
    for i in range(5):
        client.post("/v1/filters", headers=KEY, json={"name": f"F{i}", "inputs": [{"type": "UNIVERSE"}]})
    first = client.get("/v1/activity?action=FILTER_CREATE&limit=2", headers=KEY).json()
    assert len(first["entries"]) == 2 and first["nextBeforeId"]
    second = client.get(f"/v1/activity?action=FILTER_CREATE&limit=2&beforeId={first['nextBeforeId']}", headers=KEY).json()
    assert {e["id"] for e in first["entries"]}.isdisjoint({e["id"] for e in second["entries"]})
    actions = {a["action"]: a["count"] for a in client.get("/v1/activity/actions", headers=KEY).json()["actions"]}
    assert actions["FILTER_CREATE"] == 5 and actions["APP_STARTED"] == 1
    one = client.get(f"/v1/activity/{first['entries'][0]['id']}", headers=KEY).json()
    assert one["action"] == "FILTER_CREATE"
    assert client.get("/v1/activity/999999", headers=KEY).status_code == 404


def test_logging_never_breaks_the_operation(client, monkeypatch):
    from stocksvest_app.audit import store
    monkeypatch.setattr(store, "_dump", lambda v: (_ for _ in ()).throw(RuntimeError("disk full")))
    r = client.post("/v1/filters", headers=KEY, json={"name": "Still works", "inputs": [{"type": "UNIVERSE"}]})
    assert r.status_code == 201


def test_login_proxy_user_is_trusted_only_from_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    monkeypatch.setenv("APP_TRUSTED_PROXIES", "testclient")  # TestClient's address stands in for the VPS
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket())) as proxied:
        assert proxied.get("/v1/filters").status_code == 401  # no key, no login
        assert proxied.get("/v1/filters", headers={"Remote-User": "saeed"}).status_code == 200
        r = proxied.post("/v1/filters", headers={"Remote-User": "saeed"},
                         json={"name": "F", "inputs": [{"type": "UNIVERSE"}]})
        entry = proxied.get("/v1/activity?action=FILTER_CREATE", headers=KEY).json()["entries"][0]
        assert r.status_code == 201 and entry["actor"] == "user:saeed"
    monkeypatch.setenv("APP_TRUSTED_PROXIES", "100.96.27.63")  # requests from anywhere else can't claim a user
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket())) as direct:
        assert direct.get("/v1/filters", headers={"Remote-User": "saeed"}).status_code == 401
        assert direct.get("/v1/filters", headers=KEY).status_code == 200
