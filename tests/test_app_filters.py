from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fake_market import FakeMarket
from stocksvest_app import db as app_db
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings
from stocksvest_app.filters.engine import Evaluator
from stocksvest_app.filters.models import FilterIn
from stocksvest_app.market.data import MarketData
from stocksvest_app.runs import store as runs_store

KEY = {"X-API-Key": "app-key"}
PRICE_10_TO_50 = {"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "BETWEEN", "value": 10, "maxValue": 50}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    return Settings()


@pytest.fixture
def market():
    return FakeMarket()


@pytest.fixture
def client(settings, market):
    with TestClient(create_app(settings, market_factory=lambda s: market)) as c:
        yield c


def flt(name="F", inputs=None, actions=None):
    return {"name": name, "inputs": inputs or [{"type": "UNIVERSE"}], "actions": actions or []}


def tickers(resp) -> set[str]:
    return {s["ticker"] for s in resp.json()["symbols"]}


def test_auth_and_ui(client):
    assert client.get("/v1/filters").status_code == 401
    assert client.get("/v1/filters", headers=KEY).json() == {"filters": []}
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/ui/").status_code == 200


def test_crud_and_validation(client):
    r = client.post("/v1/filters", headers=KEY, json=flt(actions=[PRICE_10_TO_50]))
    assert r.status_code == 201
    fid = r.json()["id"]
    assert fid.startswith("flt_") and r.json()["actions"][0]["maxValue"] == 50

    r = client.post("/v1/filters", headers=KEY, json=flt(actions=[{**PRICE_10_TO_50, "maxValue": None}]))
    assert r.status_code == 422 and "BETWEEN needs maxValue" in r.text
    r = client.post("/v1/filters", headers=KEY, json=flt(actions=[
        {"type": "PRICE_CHANGE", "periodAmount": 20000, "periodUnit": "MINUTES", "valueType": "PERCENT",
         "condition": "MORE_THAN", "value": 1}]))
    assert r.status_code == 422 and "7 days" in r.text
    assert client.post("/v1/filters", headers=KEY, json=flt(inputs=[{"type": "FILTER"}])).status_code == 422
    r = client.post("/v1/filters", headers=KEY, json=flt(inputs=[{"type": "FILTER", "id": "flt_missing"}]))
    assert r.status_code == 422 and "does not exist" in r.text

    assert client.put(f"/v1/filters/{fid}", headers=KEY, json=flt(name="Renamed")).json()["name"] == "Renamed"
    assert client.get(f"/v1/filters/{fid}", headers=KEY).json()["actions"] == []
    assert client.delete(f"/v1/filters/{fid}", headers=KEY).status_code == 204
    assert client.get(f"/v1/filters/{fid}", headers=KEY).status_code == 404


def test_actions_narrow_in_order(client):
    body = flt(actions=[
        {"type": "SYMBOL_TYPE", "assetTypes": ["COMMON_STOCK"]},
        {"type": "SECTOR", "sectorIds": ["TECHNOLOGY", "HEALTHCARE"]},
        PRICE_10_TO_50,
        {"type": "PRICE_CHANGE", "periodAmount": 5, "periodUnit": "DAYS", "valueType": "PERCENT",
         "condition": "MORE_THAN", "value": 0},
    ])
    out = client.post("/v1/filters/preview", headers=KEY, json=body).json()
    assert [s["count"] for s in out["steps"]] == [4, 3, 2, 2, 1]
    assert [s["step"] for s in out["steps"]] == ["INPUTS", "SYMBOL_TYPE", "SECTOR", "SYMBOL_PRICE", "PRICE_CHANGE"]
    assert out["symbols"] == [{"ticker": "AAA", "id": "id-AAA", "name": "Aaa Inc", "sector": "TECHNOLOGY",
                               "price": 10.0, "priceTime": "t", "dailyChangePercent": 11.1}]


def test_market_calls_are_batched(client, market):
    client.post("/v1/filters/preview", headers=KEY, json=flt(actions=[
        PRICE_10_TO_50,
        {"type": "PRICE_CHANGE", "periodAmount": 5, "periodUnit": "DAYS", "valueType": "PERCENT",
         "condition": "MORE_THAN", "value": 0}]))
    assert ("snapshots", ["AAA", "BBB", "CCC", "DDD"]) in market.calls  # one call for all symbols
    assert ("price_change", 5, "DAYS", ["AAA", "BBB"]) in market.calls  # only the survivors
    assert [c for c in market.calls if c[0] == "snapshots"] == [("snapshots", ["AAA", "BBB", "CCC", "DDD"])]  # reused


def test_comparisons(client):
    def run(action):
        return tickers(client.post("/v1/filters/preview", headers=KEY, json=flt(actions=[action])))

    assert run({"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "LESS_THAN", "value": 10}) == {"CCC"}
    assert run({"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "MORE_THAN", "value": 10}) == {"BBB"}
    assert run(PRICE_10_TO_50) == {"AAA", "BBB"}  # inclusive; DDD has no price
    assert run({"type": "PRICE_CHANGE", "periodAmount": 1, "periodUnit": "WEEKS", "valueType": "ABSOLUTE",
                "condition": "LESS_THAN", "value": 0}) == {"BBB"}


def test_last_close_uses_previous_close_while_session_is_open(settings):
    conn = app_db.connect(settings.db_path)
    app_db.init(conn)
    f = FilterIn.model_validate(flt(actions=[
        {"type": "SYMBOL_PRICE", "priceReference": "LAST_CLOSE", "condition": "BETWEEN", "value": 55, "maxValue": 65}]))
    now = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)  # 11:00 New York
    session_open = MarketData(FakeMarket(regular_close="2026-10-16T16:00:00-04:00"))
    assert Evaluator(conn, session_open, now).evaluate(f)["symbols"] == {"BBB"}  # previousClose 60
    closed = MarketData(FakeMarket(regular_close="2026-10-16T10:00:00-04:00"))
    assert Evaluator(conn, closed, now).evaluate(f)["symbols"] == set()  # dailyClose 50


def test_filter_and_group_inputs_union_and_cycles(client):
    tech = client.post("/v1/filters", headers=KEY, json=flt("Tech", actions=[
        {"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}])).json()
    cheap = client.post("/v1/filters", headers=KEY, json=flt("Cheap", actions=[
        {"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "LESS_THAN", "value": 20}])).json()
    group = client.post("/v1/filter-groups", headers=KEY,
                        json={"name": "G", "filterIds": [tech["id"], cheap["id"]]}).json()

    on_filter = client.post("/v1/filters/preview", headers=KEY, json=flt(inputs=[{"type": "FILTER", "id": tech["id"]}]))
    assert tickers(on_filter) == {"AAA", "CCC"}
    on_group = client.post("/v1/filters/preview", headers=KEY,
                           json=flt(inputs=[{"type": "FILTER_GROUP", "id": group["id"]}]))
    assert tickers(on_group) == {"AAA", "CCC"}  # union of {AAA, CCC} and {AAA, CCC}

    # Cycles: Tech -> Cheap -> Tech, and Tech -> group G (which contains Tech)
    client.put(f"/v1/filters/{cheap['id']}", headers=KEY, json=flt("Cheap", inputs=[{"type": "FILTER", "id": tech["id"]}]))
    r = client.put(f"/v1/filters/{tech['id']}", headers=KEY, json=flt("Tech", inputs=[{"type": "FILTER", "id": cheap["id"]}]))
    assert r.status_code == 422 and "cycle" in r.text
    r = client.put(f"/v1/filters/{tech['id']}", headers=KEY,
                   json=flt("Tech", inputs=[{"type": "FILTER_GROUP", "id": group["id"]}]))
    assert r.status_code == 422 and "cycle" in r.text

    r = client.delete(f"/v1/filters/{tech['id']}", headers=KEY)  # in use
    assert r.status_code == 409 and "Cheap" in r.text and "G" in r.text


def test_group_member_cycle(client):
    a = client.post("/v1/filters", headers=KEY, json=flt("A")).json()
    g = client.post("/v1/filter-groups", headers=KEY, json={"name": "G", "filterIds": []}).json()
    client.put(f"/v1/filters/{a['id']}", headers=KEY, json=flt("A", inputs=[{"type": "FILTER_GROUP", "id": g["id"]}]))
    r = client.put(f"/v1/filter-groups/{g['id']}", headers=KEY, json={"name": "G", "filterIds": [a["id"]]})
    assert r.status_code == 422 and "cycle" in r.text


def test_unavailable_inputs(client):
    r = client.post("/v1/filters/preview", headers=KEY, json=flt(inputs=[{"type": "WATCHLIST", "id": "w1"}]))
    assert r.status_code == 422 and "not available yet" in r.text


def test_runs_are_stored_with_prices_and_definition(client):
    f = client.post("/v1/filters", headers=KEY, json=flt("Cheap", actions=[PRICE_10_TO_50])).json()
    run = client.post(f"/v1/filters/{f['id']}/run", headers=KEY).json()
    assert run["status"] == "SUCCEEDED" and run["kind"] == "FILTER"
    assert run["definition"]["actions"] == [PRICE_10_TO_50]
    assert {s["ticker"]: s["price"] for s in run["result"]["symbols"]} == {"AAA": 10.0, "BBB": 50.0}

    history = client.get(f"/v1/filters/{f['id']}/runs", headers=KEY).json()["runs"]
    assert [h["id"] for h in history] == [run["id"]] and "result" not in history[0]
    assert client.get(f"/v1/runs/{run['id']}", headers=KEY).json()["result"]["steps"][-1]["count"] == 2
    assert len(client.get("/v1/runs?kind=FILTER", headers=KEY).json()["runs"]) == 1


def test_group_run(client):
    a = client.post("/v1/filters", headers=KEY, json=flt("ADR", actions=[{"type": "SYMBOL_TYPE", "assetTypes": ["ADR"]}])).json()
    b = client.post("/v1/filters", headers=KEY, json=flt("Health", actions=[{"type": "SECTOR", "sectorIds": ["HEALTHCARE"]}])).json()
    g = client.post("/v1/filter-groups", headers=KEY, json={"name": "Mix", "filterIds": [a["id"], b["id"]]}).json()
    run = client.post(f"/v1/filter-groups/{g['id']}/run", headers=KEY).json()
    assert [m["count"] for m in run["result"]["filters"]] == [1, 1]
    assert [s["ticker"] for s in run["result"]["symbols"]] == ["BBB", "CCC"]
    assert client.get(f"/v1/filter-groups/{g['id']}/runs", headers=KEY).json()["runs"][0]["kind"] == "FILTER_GROUP"


def test_failed_run_is_recorded(settings):
    with TestClient(create_app(settings, market_factory=lambda s: FakeMarket(fail=True))) as c:
        f = c.post("/v1/filters", headers=KEY, json=flt()).json()
        assert c.post(f"/v1/filters/{f['id']}/run", headers=KEY).status_code == 502
        runs = c.get("/v1/runs", headers=KEY).json()["runs"]
        assert runs[0]["status"] == "FAILED" and "unreachable" in runs[0]["error"]


def test_retention(settings):
    conn = app_db.connect(settings.db_path)
    app_db.init(conn)
    conn.execute("INSERT INTO runs VALUES ('old', 'FILTER', 'f', '2020-01-01T00:00:00Z', 1, 'SUCCEEDED', '{}', NULL, NULL)")
    conn.commit()
    new = runs_store.add_run(conn, "FILTER", "f", {}, 1, result={}, retention_days=183)
    assert [r["id"] for r in runs_store.list_runs(conn)] == [new["id"]]  # the 2020 run was pruned


def test_filter_actions_metadata(client):
    meta = client.get("/v1/filter-actions", headers=KEY).json()
    sector = next(a for a in meta["actions"] if a["type"] == "SECTOR")
    assert sector["fields"][0]["options"] == [{"value": "TECHNOLOGY", "label": "Technology"}]
    assert {t["type"]: t["available"] for t in meta["inputTypes"]}["WATCHLIST"] is False
