"""The POSITION filter action: keep symbols held in one or more portfolios."""

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings

KEY = {"X-API-Key": "app-key"}


@pytest.fixture
def broker():
    return FakeBroker(cash="10000")


@pytest.fixture
def client(tmp_path, monkeypatch, broker):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    market = FakeMarket()
    with TestClient(create_app(Settings(), market_factory=lambda s: market, broker_factory=lambda a: broker)) as c:
        yield c


@pytest.fixture
def portfolios(client, broker):
    """Two portfolios on one account: A holds AAA and BBB, B holds CCC."""
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    a, b = (client.post("/v1/portfolios", headers=KEY,
                        json={"name": name, "brokerAccountId": acct["id"], "capital": 3000}).json() for name in "AB")
    for p, symbol in ((a, "AAA"), (a, "BBB"), (b, "CCC")):
        order = client.post(f"/v1/portfolios/{p['id']}/orders", headers=KEY,
                            json={"symbol": symbol, "side": "BUY", "quantity": 2}).json()
        broker.fill(order["brokerOrderId"], 2, 10)
    client.post(f"/v1/portfolios/{a['id']}/sync", headers=KEY)
    return a, b


def position(*ids):
    return {"type": "POSITION", "portfolioIds": list(ids)}


def preview(client, actions):
    r = client.post("/v1/filters/preview", headers=KEY,
                    json={"name": "F", "inputs": [{"type": "UNIVERSE"}], "actions": actions})
    assert r.status_code == 200, r.text
    return r.json()


def tickers(out) -> set[str]:
    return {s["ticker"] for s in out["symbols"]}


def test_one_and_several_portfolios(client, portfolios):
    a, b = portfolios
    out = preview(client, [position(a["id"])])
    assert tickers(out) == {"AAA", "BBB"}
    assert out["steps"] == [{"step": "INPUTS", "count": 4}, {"step": "POSITION", "count": 2}]
    assert tickers(preview(client, [position(a["id"], b["id"])])) == {"AAA", "BBB", "CCC"}  # union


def test_combines_with_other_actions(client, portfolios):
    a, b = portfolios
    tech = {"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}
    assert tickers(preview(client, [position(a["id"], b["id"]), tech])) == {"AAA", "CCC"}


def test_sold_out_positions_drop_out(client, portfolios, broker):
    a, _ = portfolios
    order = client.post(f"/v1/portfolios/{a['id']}/orders", headers=KEY,
                        json={"symbol": "BBB", "side": "SELL", "quantity": 2}).json()
    broker.fill(order["brokerOrderId"], 2, 12)
    client.post(f"/v1/portfolios/{a['id']}/sync", headers=KEY)
    assert tickers(preview(client, [position(a["id"])])) == {"AAA"}


def test_validation(client, portfolios):
    a, _ = portfolios
    body = lambda actions: {"name": "F", "inputs": [{"type": "UNIVERSE"}], "actions": actions}  # noqa: E731
    r = client.post("/v1/filters", headers=KEY, json=body([position()]))
    assert r.status_code == 422
    r = client.post("/v1/filters", headers=KEY, json=body([position(a["id"], a["id"])]))
    assert r.status_code == 422 and "listed twice" in r.text
    r = client.post("/v1/filters", headers=KEY, json=body([position("pf_missing")]))
    assert r.status_code == 422 and "pf_missing" in r.text


def test_editor_metadata(client):
    meta = lambda: next(a for a in client.get("/v1/filter-actions", headers=KEY).json()["actions"]  # noqa: E731
                        if a["type"] == "POSITION")
    assert meta()["available"] is False and meta()["fields"][0]["options"] == []
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    p = client.post("/v1/portfolios", headers=KEY, json={"name": "Main", "brokerAccountId": acct["id"]}).json()
    field = meta()["fields"][0]
    assert meta()["available"] is True
    assert field["kind"] == "multiselect" and field["options"] == [{"value": p["id"], "label": "Main"}]


def test_portfolio_used_by_a_filter_cannot_be_deleted(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    p = client.post("/v1/portfolios", headers=KEY, json={"name": "Empty", "brokerAccountId": acct["id"]}).json()
    f = client.post("/v1/filters", headers=KEY, json={"name": "Held", "inputs": [{"type": "UNIVERSE"}],
                                                       "actions": [position(p["id"])]}).json()
    r = client.delete(f"/v1/portfolios/{p['id']}", headers=KEY)
    assert r.status_code == 409 and "Held" in r.text
    client.delete(f"/v1/filters/{f['id']}", headers=KEY)
    assert client.delete(f"/v1/portfolios/{p['id']}", headers=KEY).status_code == 204


def test_saved_filter_run(client, portfolios):
    a, _ = portfolios
    f = client.post("/v1/filters", headers=KEY, json={"name": "Held in A", "inputs": [{"type": "UNIVERSE"}],
                                                       "actions": [position(a["id"])]}).json()
    run = client.post(f"/v1/filters/{f['id']}/run", headers=KEY).json()
    assert {s["ticker"] for s in run["result"]["symbols"]} == {"AAA", "BBB"}

