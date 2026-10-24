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
def market():
    return FakeMarket()


@pytest.fixture
def client(tmp_path, monkeypatch, broker, market):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
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


# ------------------------------------------------------------------ UNREALIZED_PNL action
# All bought at $10: AAA is now $10 (0%), BBB $50 (+400%, +$80), CCC $5 (-50%, -$10).


def pnl(ids, condition, value, value_type="PERCENT", max_value=None):
    return {"type": "UNREALIZED_PNL", "portfolioIds": list(ids), "valueType": value_type,
            "condition": condition, "value": value, "maxValue": max_value}


def test_unrealized_pnl_percent_and_dollars(client, portfolios):
    a, b = portfolios
    both = (a["id"], b["id"])
    out = preview(client, [pnl(both, "LESS_THAN", -8)])
    assert tickers(out) == {"CCC"} and out["steps"] == [{"step": "INPUTS", "count": 4},
                                                         {"step": "UNREALIZED_PNL", "count": 1}]
    assert tickers(preview(client, [pnl(both, "MORE_THAN", 100)])) == {"BBB"}
    assert tickers(preview(client, [pnl(both, "BETWEEN", -1, max_value=1)])) == {"AAA"}
    assert tickers(preview(client, [pnl(both, "MORE_THAN", 50, "ABSOLUTE")])) == {"BBB"}
    assert tickers(preview(client, [pnl([a["id"]], "LESS_THAN", 0)])) == set()  # CCC is only held in B


def test_unrealized_pnl_combines_the_portfolios_shares(client, portfolios, broker):
    a, b = portfolios
    order = client.post(f"/v1/portfolios/{a['id']}/orders", headers=KEY,
                        json={"symbol": "CCC", "side": "BUY", "quantity": 2}).json()
    broker.fill(order["brokerOrderId"], 2, 2.5)  # A's CCC is up 100%, B's down 50%: together cost $25, worth $20
    client.post(f"/v1/portfolios/{a['id']}/sync", headers=KEY)
    assert tickers(preview(client, [pnl([a["id"]], "MORE_THAN", 50)])) == {"BBB", "CCC"}
    assert tickers(preview(client, [pnl([a["id"], b["id"]], "BETWEEN", -21, max_value=-19)])) == {"CCC"}


def test_unrealized_pnl_uses_live_last_trades(client, portfolios, market, monkeypatch):
    a, b = portfolios
    both = (a["id"], b["id"])
    market.live = {"AAA": {"bid": 8.9, "ask": 9.1, "last": 9.0, "price": 9.0},  # live -10%, delayed 0%
                   "BBB": {"bid": 49.0, "ask": 49.5, "last": None, "price": 49.25}}  # no trade yet: delayed
    assert tickers(preview(client, [pnl(both, "LESS_THAN", -8)])) == {"AAA", "CCC"}
    assert tickers(preview(client, [pnl(both, "MORE_THAN", 100)])) == {"BBB"}
    asked = [c for c in market.calls if c[0] == "quotes"]
    assert asked[-1] == ("quotes", ["AAA", "BBB", "CCC"])  # once per evaluation, only the held symbols

    from stocksvest_app.market.client import MarketError

    def down(symbols):
        raise MarketError(502, "market service unreachable")
    monkeypatch.setattr(market, "quotes", down)
    assert tickers(preview(client, [pnl(both, "LESS_THAN", -8)])) == {"CCC"}  # delayed prices instead


def test_unrealized_pnl_validation_and_metadata(client, portfolios):
    a, _ = portfolios
    body = lambda actions: {"name": "F", "inputs": [{"type": "UNIVERSE"}], "actions": actions}  # noqa: E731
    assert client.post("/v1/filters", headers=KEY, json=body([pnl([], "LESS_THAN", -8)])).status_code == 422
    r = client.post("/v1/filters", headers=KEY, json=body([pnl(["pf_missing"], "LESS_THAN", -8)]))
    assert r.status_code == 422 and "pf_missing" in r.text
    r = client.post("/v1/filters", headers=KEY, json=body([pnl([a["id"]], "BETWEEN", -8)]))
    assert r.status_code == 422 and "maxValue" in r.text
    empty = client.post("/v1/portfolios", headers=KEY, json={"name": "Empty", "brokerAccountId": a["brokerAccountId"]})
    f = client.post("/v1/filters", headers=KEY,
                    json={**body([pnl([empty.json()["id"]], "LESS_THAN", -8)]), "name": "Stop loss"}).json()
    r = client.delete(f"/v1/portfolios/{empty.json()['id']}", headers=KEY)
    assert r.status_code == 409 and "Stop loss" in r.text
    client.delete(f"/v1/filters/{f['id']}", headers=KEY)
    meta = next(m for m in client.get("/v1/filter-actions", headers=KEY).json()["actions"]
                if m["type"] == "UNREALIZED_PNL")
    assert meta["available"] and [f["name"] for f in meta["fields"]] == [
        "portfolioIds", "valueType", "condition", "value", "maxValue"]


def test_held_symbols_get_live_bars(client, portfolios, market):
    from types import SimpleNamespace

    from stocksvest_app import db
    from stocksvest_app.automations.scheduler import watch_held_symbols

    conn = db.connect(Settings().db_path)
    try:
        assert watch_held_symbols(SimpleNamespace(market_api=market), conn) == ["AAA", "BBB", "CCC"]
        assert market.calls[-1] == ("live_watch", ["AAA", "BBB", "CCC"], 10)
        market.fail = True  # the market service being down never breaks the scheduler pass
        assert watch_held_symbols(SimpleNamespace(market_api=market), conn) == ["AAA", "BBB", "CCC"]
    finally:
        conn.close()


# ------------------------------------------------------------------ INVESTMENT_PORTFOLIO input


def from_inputs(client, inputs, actions=()):
    r = client.post("/v1/filters/preview", headers=KEY, json={"name": "F", "inputs": inputs, "actions": list(actions)})
    assert r.status_code == 200, r.text
    return r.json()


def test_portfolio_input(client, portfolios):
    a, b = portfolios
    out = from_inputs(client, [{"type": "INVESTMENT_PORTFOLIO", "id": a["id"]}])
    assert tickers(out) == {"AAA", "BBB"} and out["steps"] == [{"step": "INPUTS", "count": 2}]
    both = [{"type": "INVESTMENT_PORTFOLIO", "id": a["id"]}, {"type": "INVESTMENT_PORTFOLIO", "id": b["id"]}]
    assert tickers(from_inputs(client, both)) == {"AAA", "BBB", "CCC"}  # inputs are a union
    price_above_20 = {"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "MORE_THAN", "value": 20}
    assert tickers(from_inputs(client, both, [price_above_20])) == {"BBB"}


def test_portfolio_input_keeps_holdings_that_left_the_universe(client, portfolios):
    from stocksvest_app import db
    from stocksvest_app.portfolios import store as pf

    a, _ = portfolios
    conn = db.connect(Settings().db_path)
    with conn:  # shares of a symbol that is no longer halal (the market doesn't know it any more)
        pf.open_lot(conn, a["id"], "ZZZ", None, pf.D(3), pf.D(4))
    out = from_inputs(client, [{"type": "INVESTMENT_PORTFOLIO", "id": a["id"]}])
    rows = {s["ticker"]: s for s in out["symbols"]}
    assert set(rows) == {"AAA", "BBB", "ZZZ"} and rows["ZZZ"]["price"] is None and rows["ZZZ"]["name"] is None
    tech = {"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}
    assert tickers(from_inputs(client, [{"type": "INVESTMENT_PORTFOLIO", "id": a["id"]}], [tech])) == {"AAA"}
    portfolio = client.get(f"/v1/portfolios/{a['id']}", headers=KEY).json()  # other prices still load
    prices = {p["symbol"]: p["price"] for p in portfolio["positions"]}
    assert prices["AAA"] == 10 and prices["ZZZ"] is None


def test_portfolio_input_validation_and_delete_guard(client):
    acct = client.post("/v1/broker-accounts", headers=KEY, json={"name": "Paper", "provider": "ALPACA"}).json()
    p = client.post("/v1/portfolios", headers=KEY, json={"name": "Empty", "brokerAccountId": acct["id"]}).json()
    r = client.post("/v1/filters", headers=KEY, json={"name": "F", "inputs": [{"type": "INVESTMENT_PORTFOLIO"}]})
    assert r.status_code == 422 and "needs an id" in r.text
    r = client.post("/v1/filters", headers=KEY,
                    json={"name": "F", "inputs": [{"type": "INVESTMENT_PORTFOLIO", "id": "pf_missing"}]})
    assert r.status_code == 422 and "pf_missing" in r.text
    f = client.post("/v1/filters", headers=KEY,
                    json={"name": "Mine", "inputs": [{"type": "INVESTMENT_PORTFOLIO", "id": p["id"]}]}).json()
    assert tickers(client.post(f"/v1/filters/{f['id']}/run", headers=KEY).json()["result"]) == set()  # nothing held
    r = client.delete(f"/v1/portfolios/{p['id']}", headers=KEY)
    assert r.status_code == 409 and "Mine" in r.text
    meta = client.get("/v1/filter-actions", headers=KEY).json()["inputTypes"]
    entry = next(t for t in meta if t["type"] == "INVESTMENT_PORTFOLIO")
    assert entry["available"] and entry["options"] == [{"value": p["id"], "label": "Empty"}]
