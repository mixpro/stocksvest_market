"""Every endpoint, from the app's own OpenAPI description: auth, unknown ids, and bad input never cause a 500."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings

KEY = {"X-API-Key": "app-key"}
PUBLIC = {"/health"}
GARBAGE_BODIES = [
    None, {}, [], "text", 12, {"name": None}, {"name": "x" * 10000}, {"name": "💥", "inputs": "nope"},
    {"amount": -1}, {"amount": "lots"}, {"inputs": [{"type": "MARS"}]}, {"schedules": [{"type": "DAILY"}]},
    {"symbol": "AAA", "side": "HOLD", "quantity": 1}, {"automationsEnabled": "maybe"}, {"filterIds": [None]},
    {"actions": [{"type": "PLACE_ORDER", "side": "BUY", "sizings": []}]},
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("DATA_DIR", str(tmp_path_factory.mktemp("contract")))
    mp.setenv("APP_API_KEYS", "app-key")
    mp.setenv("APP_RUN_SCHEDULER", "false")
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket(),
                               broker_factory=lambda a: FakeBroker()), raise_server_exceptions=False) as c:
        yield c
    mp.undo()


def routes(client) -> list[tuple[str, str]]:
    spec = client.get("/openapi.json").json()
    return [(method.upper(), path) for path, ops in spec["paths"].items() for method in ops
            if path.startswith("/v1/")]


def concrete(path: str) -> str:
    """Fill path parameters with ids that don't exist."""
    def value(m):
        name = m.group(1)
        return "999999" if name == "entry_id" else "AAA" if name in ("symbol", "path") else f"x_does_not_exist_{name}"
    return re.sub(r"\{(\w+)(?::path)?\}", value, path)


def call(client, method, path, body=None, headers=KEY, raw: bytes | None = None):
    kwargs = {"headers": dict(headers)}
    if raw is not None:
        kwargs["content"] = raw
        kwargs["headers"]["Content-Type"] = "application/json"
    elif body is not None:
        kwargs["json"] = body
    return client.request(method, concrete(path), **kwargs)


def test_the_api_has_the_expected_surface(client):
    found = routes(client)
    assert len(found) > 60, found  # filters, groups, automations, portfolios, brokers, orders, history, activity, market
    assert ("POST", "/v1/automations/{automation_id}/run") in found


def test_every_endpoint_needs_a_key(client):
    for method, path in routes(client):
        for headers in ({}, {"X-API-Key": "wrong"}):
            r = call(client, method, path, headers=headers)
            assert r.status_code == 401, (method, path, r.status_code, r.text[:200])


def test_unknown_ids_are_404_never_500(client):
    for method, path in routes(client):
        if "{" not in path or path.startswith("/v1/market/"):
            continue
        r = call(client, method, path, body={})
        assert r.status_code in (404, 422), (method, path, r.status_code, r.text[:200])


def test_garbage_input_is_rejected_cleanly(client):
    for method, path in routes(client):
        if method not in ("POST", "PUT", "PATCH"):
            continue
        for body in GARBAGE_BODIES:
            r = call(client, method, path, body=body)
            assert r.status_code < 500, (method, path, body, r.status_code, r.text[:300])
        for raw in (b"{not json", b"\x00\xff", b"[" * 5000):
            r = call(client, method, path, raw=raw)
            assert r.status_code < 500, (method, path, raw[:20], r.status_code, r.text[:300])


def test_bad_query_parameters_are_rejected_cleanly(client):
    for method, path in routes(client):
        if method != "GET":
            continue
        for query in ("limit=-5", "limit=abc", "limit=99999999", "start=yesterday", "since=%00", "kind=WEEKLY",
                      "period=forever", "timeframe=2Days", "status=" + "x" * 3000):
            r = client.get(f"{concrete(path)}?{query}", headers=KEY)
            assert r.status_code < 500, (path, query, r.status_code, r.text[:300])


def test_wrong_methods_are_405(client):
    assert client.patch("/v1/filters", headers=KEY).status_code == 405
    assert client.delete("/v1/activity", headers=KEY).status_code == 405
    assert client.post("/v1/notifications", headers=KEY).status_code == 405


def test_every_failure_has_a_readable_detail(client):
    for method, path in routes(client):
        if "{" not in path or path.startswith("/v1/market/"):
            continue
        r = call(client, method, path, body={})
        detail = r.json().get("detail")
        assert detail, (method, path, r.text[:200])
        assert isinstance(detail, (str, list)), (method, path, detail)
