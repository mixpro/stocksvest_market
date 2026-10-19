"""End-to-end scenarios through the API only: build whole app setups, restart, break dependencies, run out of
money, lose the market or the broker, and hammer endpoints concurrently. Orders are left out on purpose."""

import random
import threading
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app import db
from stocksvest_app.api import create_app
from stocksvest_app.automations import scheduler
from stocksvest_app.config import Settings
from stocksvest_app.deps import AppContext

KEY = {"X-API-Key": "app-key"}
ET = ZoneInfo("America/New_York")


class Env:
    """One app on a data folder, with a fake market and broker that tests can break and fix."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("APP_API_KEYS", "app-key")
        monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
        self.market = FakeMarket(last_sync=datetime.now(timezone.utc).isoformat())
        self.broker = FakeBroker(cash="100000")
        self.client = None
        self.start()

    def start(self):
        self.app = create_app(Settings(), market_factory=lambda s: self.market, broker_factory=lambda a: self.broker)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()

    def restart(self):
        self.client.__exit__(None, None, None)
        self.start()

    def stop(self):
        self.client.__exit__(None, None, None)

    # -- API helpers (assert success unless told otherwise)

    def call(self, method, path, body=None, expect=None):
        r = self.client.request(method, path, headers=KEY, json=body)
        if expect is None:
            assert r.status_code < 300, (method, path, r.status_code, r.text[:400])
        else:
            assert r.status_code == expect, (method, path, r.status_code, r.text[:400])
        return r.json() if r.content else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)

    def filter(self, name, inputs=None, actions=None):
        return self.post("/v1/filters", {"name": name, "inputs": inputs or [{"type": "UNIVERSE"}], "actions": actions or []})

    def automation(self, name, targets, schedules=None, actions=None):
        return self.post("/v1/automations", {
            "name": name, "runTargets": targets,
            "schedules": schedules or [{"type": "DAILY", "atTimes": ["10:00"]}],
            "actions": actions or [{"type": "NOTIFY", "title": "{name}: {count}"}]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.stop()


def tickers(result) -> set[str]:
    return {s["ticker"] for s in result["symbols"]}


# ------------------------------------------------------------------ filters, groups, automations


def test_full_setup_then_teardown_in_dependency_order(env):
    tech = env.filter("Tech", actions=[{"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}])
    cheap = env.filter("Cheap", actions=[{"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR",
                                          "condition": "LESS_THAN", "value": 20}])
    cheap_tech = env.filter("Cheap tech", inputs=[{"type": "FILTER", "id": tech["id"]}],
                            actions=[{"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR",
                                      "condition": "LESS_THAN", "value": 20}])
    group = env.post("/v1/filter-groups", {"name": "Scans", "filterIds": [tech["id"], cheap["id"]]})
    on_group = env.filter("From group", inputs=[{"type": "FILTER_GROUP", "id": group["id"]}])
    assert tickers(env.post(f"/v1/filters/{cheap_tech['id']}/run")["result"]) == {"AAA", "CCC"}
    assert tickers(env.post(f"/v1/filter-groups/{group['id']}/run")["result"]) == {"AAA", "CCC"}  # union
    assert tickers(env.post(f"/v1/filters/{on_group['id']}/run")["result"]) == {"AAA", "CCC"}

    auto = env.automation("Watch", [{"type": "FILTER", "id": cheap_tech["id"]},
                                    {"type": "FILTER_GROUP", "id": group["id"]}])
    run = env.post(f"/v1/automations/{auto['id']}/run")
    assert run["status"] == "SUCCEEDED" and run["result"]["metrics"]["matchedCount"] == 2
    assert env.get("/v1/notifications")["unread"] == 1

    # Everything in use refuses deletion, with a reason naming the user.
    assert "Watch" in env.delete(f"/v1/filters/{cheap_tech['id']}", expect=409)["detail"]
    assert "Cheap tech" in env.delete(f"/v1/filters/{tech['id']}", expect=409)["detail"]
    detail = env.delete(f"/v1/filter-groups/{group['id']}", expect=409)["detail"]
    assert "From group" in detail
    # Tear down in order.
    env.delete(f"/v1/automations/{auto['id']}")
    env.delete(f"/v1/filters/{on_group['id']}")
    env.delete(f"/v1/filter-groups/{group['id']}")
    env.delete(f"/v1/filters/{cheap_tech['id']}")
    for f in (tech, cheap):
        env.delete(f"/v1/filters/{f['id']}")
    assert env.get("/v1/filters")["filters"] == [] and env.get("/v1/automations")["automations"] == []
    # History outlives the things it is about.
    assert len(env.get(f"/v1/automations/{auto['id']}/runs")["runs"]) == 1
    assert len(env.get(f"/v1/filters/{cheap_tech['id']}/runs")["runs"]) == 1
    assert [e["type"] for e in env.get(f"/v1/automations/{auto['id']}/events")["events"]][0] == "DELETED"


def test_deep_cycles_are_refused(env):
    a = env.filter("A")
    b = env.filter("B", inputs=[{"type": "FILTER", "id": a["id"]}])
    g = env.post("/v1/filter-groups", {"name": "G", "filterIds": [b["id"]]})
    c = env.filter("C", inputs=[{"type": "FILTER_GROUP", "id": g["id"]}])
    # A -> C -> G -> B -> A would loop.
    r = env.put(f"/v1/filters/{a['id']}", {"name": "A", "inputs": [{"type": "FILTER", "id": c["id"]}]}, expect=422)
    assert "cycle" in r["detail"]
    assert env.post(f"/v1/filters/{c['id']}/run")["status"] == "SUCCEEDED"  # the graph is still usable


def test_automation_lifecycle(env):
    f = env.filter("All")
    a = env.automation("Daily", [{"type": "FILTER", "id": f["id"]}])
    assert a["status"] == "ACTIVE" and a["configVersion"] == 1 and env.get(f"/v1/automations/{a['id']}")["nextRunAt"]
    env.post(f"/v1/automations/{a['id']}/pause")
    env.post(f"/v1/automations/{a['id']}/pause")  # pausing twice is fine
    assert env.get(f"/v1/automations/{a['id']}")["nextRunAt"] is None
    env.post(f"/v1/automations/{a['id']}/resume")
    body = {"name": "Daily v2", "runTargets": a["runTargets"], "schedules": a["schedules"], "actions": a["actions"]}
    assert env.put(f"/v1/automations/{a['id']}", body)["configVersion"] == 2
    assert env.post(f"/v1/automations/{a['id']}/preview")["preview"] is True
    assert env.get("/v1/notifications")["unread"] == 0  # previews send nothing
    env.post(f"/v1/automations/{a['id']}/run")
    env.post(f"/v1/automations/{a['id']}/run")
    assert env.get("/v1/notifications")["unread"] == 2
    env.post("/v1/notifications/read-all")
    assert env.get("/v1/notifications?unreadOnly=true")["notifications"] == []
    types = [e["type"] for e in env.get(f"/v1/automations/{a['id']}/events")["events"]]
    assert types == ["UPDATED", "RESUMED", "PAUSED", "PAUSED", "CREATED"]


# ------------------------------------------------------------------ restarts


def test_everything_survives_a_restart(env):
    f = env.filter("All")
    acct = env.post("/v1/broker-accounts", {"name": "Paper", "provider": "ALPACA"})
    pf = env.post("/v1/portfolios", {"name": "P", "brokerAccountId": acct["id"], "capital": 5000})
    a = env.automation("Bot", [{"type": "FILTER", "id": f["id"]}])
    b = env.post(f"/v1/portfolios/{pf['id']}/budgets", {"name": "Main", "amount": 1500, "automationIds": [a["id"]]})
    env.post(f"/v1/automations/{a['id']}/run")
    before = (env.get(f"/v1/portfolios/{pf['id']}"), env.get(f"/v1/budgets/{b['id']}"),
              env.get(f"/v1/budgets/{b['id']}/ledger"), env.get(f"/v1/automations/{a['id']}"),
              env.get(f"/v1/automations/{a['id']}/runs"), env.get("/v1/notifications"))

    env.restart()
    after = (env.get(f"/v1/portfolios/{pf['id']}"), env.get(f"/v1/budgets/{b['id']}"),
             env.get(f"/v1/budgets/{b['id']}/ledger"), env.get(f"/v1/automations/{a['id']}"),
             env.get(f"/v1/automations/{a['id']}/runs"), env.get("/v1/notifications"))
    strip = lambda p: {k: v for k, v in p.items() if k != "lastSync"}  # noqa: E731
    assert strip(after[0]) == strip(before[0]) and after[1:] == before[1:]
    assert [x["action"] for x in env.get("/v1/activity?action=APP_STARTED")["entries"]] == ["APP_STARTED"] * 2


def test_a_scheduled_time_never_runs_twice_across_restarts(env):
    f = env.filter("All")
    a = env.automation("Daily", [{"type": "FILTER", "id": f["id"]}],
                       schedules=[{"type": "DAILY", "atTimes": ["10:00"], "missedPolicy": "RUN_ONCE"}])

    def tick(*when):
        ctx, conn = AppContext(Settings(), env.market), db.connect(Settings().db_path)
        try:
            return scheduler.tick(ctx, conn, datetime(*when, tzinfo=ET))
        finally:
            conn.close()

    tick(2026, 10, 19, 9, 0)
    assert len(tick(2026, 10, 19, 10, 0, 30)) == 1
    env.restart()
    assert tick(2026, 10, 19, 10, 1) == []  # same time, new process
    assert len(tick(2026, 10, 20, 10, 0, 30)) == 1
    assert len(env.get(f"/v1/automations/{a['id']}/runs")["runs"]) == 2


# ------------------------------------------------------------------ money: capital and budgets


def check_money_invariants(env, account_id):
    """What must always hold, whatever happened before."""
    account = env.get(f"/v1/broker-accounts/{account_id}")
    assert account["assignedCash"] <= account["cash"] + 1e-6
    assert account["unassignedCash"] >= -1e-6
    for p in account["portfolios"]:
        pf = env.get(f"/v1/portfolios/{p['id']}")
        budgets = sum(b["cash"] for b in pf["budgets"])
        assert budgets <= pf["cash"] + 1e-6, "budgets exceed the portfolio's cash"
        assert pf["unallocatedCash"] == pytest.approx(pf["cash"] - budgets)
        ledger = env.get(f"/v1/portfolios/{p['id']}/ledger?limit=1000")["entries"]
        # The ledger replays to the current cash, entry by entry.
        running = Decimal(0)
        for e in reversed(ledger):
            running += Decimal(str(e["amount"]))
            assert float(running) == pytest.approx(e["cashAfter"])
        assert float(running) == pytest.approx(pf["cash"])
        for b in pf["budgets"]:
            entries = env.get(f"/v1/budgets/{b['id']}/ledger?limit=1000")["entries"]
            total = sum(Decimal(str(e["amount"])) for e in entries if e["type"] not in ("RESERVE", "RELEASE"))
            assert float(total) == pytest.approx(b["cash"])
            assert b["available"] >= -1e-6


def test_random_capital_and_budget_operations_keep_the_books_straight(env):
    rng = random.Random(42)
    acct = env.post("/v1/broker-accounts", {"name": "Paper", "provider": "ALPACA"})
    f = env.filter("All")
    portfolios = [env.post("/v1/portfolios", {"name": f"P{i}", "brokerAccountId": acct["id"], "capital": 20000})
                  for i in range(3)]
    budgets = [env.post(f"/v1/portfolios/{rng.choice(portfolios)['id']}/budgets", {"name": f"B{i}"})
               for i in range(4)]
    outcomes = {"ok": 0, "refused": 0}
    for step in range(150):
        op = rng.choice(["pf_allocate", "pf_withdraw", "budget_allocate", "budget_withdraw"])
        amount = round(rng.uniform(1, 15000), 2)
        if op.startswith("pf"):
            target = rng.choice(portfolios)["id"]
            r = env.client.post(f"/v1/portfolios/{target}/capital/{op[3:]}", headers=KEY, json={"amount": amount})
        else:
            target = rng.choice(budgets)["id"]
            r = env.client.post(f"/v1/budgets/{target}/{op[7:]}", headers=KEY, json={"amount": amount})
        assert r.status_code in (200, 422), (step, op, r.status_code, r.text)
        outcomes["ok" if r.status_code == 200 else "refused"] += 1
        if step % 25 == 0:
            check_money_invariants(env, acct["id"])
    check_money_invariants(env, acct["id"])
    assert outcomes["ok"] > 30 and outcomes["refused"] > 10  # both paths were exercised


def test_concurrent_requests_cannot_overspend(env):
    """Many clients grabbing money at the same time: the account and portfolio limits still hold."""
    acct = env.post("/v1/broker-accounts", {"name": "Paper", "provider": "ALPACA"})
    pf = env.post("/v1/portfolios", {"name": "P", "brokerAccountId": acct["id"], "capital": 10000})
    f = env.filter("All")
    budgets = [env.post(f"/v1/portfolios/{pf['id']}/budgets", {"name": f"B{i}"}) for i in range(8)]
    results = []

    def grab(path, amount):
        c = TestClient(env.app, raise_server_exceptions=False)  # its own connection, same app
        results.append(c.post(path, headers=KEY, json={"amount": amount}).status_code)

    threads = [threading.Thread(target=grab, args=(f"/v1/budgets/{b['id']}/allocate", 3000)) for b in budgets]
    threads += [threading.Thread(target=grab, args=(f"/v1/portfolios/{pf['id']}/capital/allocate", 40000))
                for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(code in (200, 422) for code in results), results
    portfolio = env.get(f"/v1/portfolios/{pf['id']}")
    budgets = sum(b["cash"] for b in portfolio["budgets"])
    assert budgets <= portfolio["cash"], f"budgets {budgets} > portfolio cash {portfolio['cash']}"
    assert portfolio["cash"] <= 100000, f"portfolio holds {portfolio['cash']} of a 100000 account"
    check_money_invariants(env, acct["id"])


# ------------------------------------------------------------------ outages


def test_market_outage_degrades_gracefully_and_recovers(env):
    f = env.filter("All")
    a = env.automation("Bot", [{"type": "FILTER", "id": f["id"]}])
    env.market.fail = True
    assert env.post("/v1/filters/preview", {"name": "x", "inputs": [{"type": "UNIVERSE"}]}, expect=502)
    assert env.post(f"/v1/filters/{f['id']}/run", expect=502)
    run = env.post(f"/v1/automations/{a['id']}/run")
    assert run["status"] == "FAILED" and "unreachable" in run["error"]
    assert env.get("/v1/market/status", expect=502)
    # Everything that doesn't need market data keeps working.
    g = env.filter("Created during the outage")
    env.post(f"/v1/automations/{a['id']}/pause")
    assert env.get(f"/v1/automations/{a['id']}")["status"] == "PAUSED"
    env.market.fail = False
    assert env.post(f"/v1/filters/{g['id']}/run")["status"] == "SUCCEEDED"
    statuses = [r["status"] for r in env.get(f"/v1/filters/{f['id']}/runs")["runs"]]
    assert statuses == ["FAILED"]  # the failed run is in the history


def test_broker_outage_leaves_nothing_half_done(env):
    acct = env.post("/v1/broker-accounts", {"name": "Paper", "provider": "ALPACA"})
    original = env.broker.account_snapshot
    from stocksvest_app.brokers.base import BrokerError
    env.broker.account_snapshot = lambda: (_ for _ in ()).throw(BrokerError("Alpaca unreachable", retryable=True))
    env.post("/v1/portfolios", {"name": "P", "brokerAccountId": acct["id"], "capital": 1000}, expect=502)
    assert env.get("/v1/portfolios")["portfolios"] == []
    assert env.get(f"/v1/broker-accounts/{acct['id']}")["status"] == "ERROR"
    env.broker.account_snapshot = original
    env.post("/v1/portfolios", {"name": "P", "brokerAccountId": acct["id"], "capital": 1000})
    assert env.get(f"/v1/broker-accounts/{acct['id']}")["status"] == "CONNECTED"


# ------------------------------------------------------------------ input edge cases


def test_names_limits_and_odd_input(env):
    assert env.filter("טכנולוגיה 💹 Tech")["name"] == "טכנולוגיה 💹 Tech"
    env.filter("x" * 100)
    env.call("POST", "/v1/filters", {"name": "x" * 101, "inputs": [{"type": "UNIVERSE"}]}, expect=422)
    env.call("POST", "/v1/filters", {"name": "dup", "inputs": [{"type": "UNIVERSE"}, {"type": "UNIVERSE"}]})
    f = env.filter("Big list", actions=[{"type": "SECTOR", "sectorIds": [f"S{i}" for i in range(500)]}])
    assert env.post(f"/v1/filters/{f['id']}/run")["result"]["symbols"] == []
    env.call("POST", "/v1/filter-groups", {"name": "G", "filterIds": [f["id"], f["id"]]}, expect=422)
    env.call("POST", "/v1/automations", {"name": "No schedule", "runTargets": [{"type": "FILTER", "id": f["id"]}],
                                         "schedules": [], "actions": [{"type": "NOTIFY", "title": "x"}]}, expect=422)
    acct = env.post("/v1/broker-accounts", {"name": "Paper", "provider": "ALPACA"})
    env.call("POST", "/v1/portfolios", {"name": "P", "brokerAccountId": acct["id"], "capital": 0.001}, expect=422)
    env.call("POST", "/v1/portfolios", {"name": "P", "brokerAccountId": acct["id"], "capital": -5}, expect=422)


# ------------------------------------------------------------------ the activity log sees everything


def test_every_change_in_a_session_is_in_the_activity_log(env):
    made = 0

    def change(method, path, body=None, expect=None):
        nonlocal made
        made += 1
        return env.call(method, path, body, expect=expect)

    f = change("POST", "/v1/filters", {"name": "F", "inputs": [{"type": "UNIVERSE"}]})
    change("PUT", f"/v1/filters/{f['id']}", {"name": "F2", "inputs": [{"type": "UNIVERSE"}]})
    a = change("POST", "/v1/automations", {"name": "A", "runTargets": [{"type": "FILTER", "id": f["id"]}],
                                           "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}],
                                           "actions": [{"type": "NOTIFY", "title": "x"}]})
    change("POST", f"/v1/automations/{a['id']}/run")
    change("POST", f"/v1/automations/{a['id']}/pause")
    change("DELETE", f"/v1/filters/{f['id']}", expect=409)
    change("DELETE", f"/v1/automations/{a['id']}")
    change("DELETE", f"/v1/filters/{f['id']}")
    change("PUT", "/v1/automations/switch", {"automationsEnabled": False})
    entries = env.get("/v1/activity?source=API&limit=1000")["entries"]
    assert len(entries) == made
    assert [e["outcome"] for e in entries].count("FAILED") == 1
