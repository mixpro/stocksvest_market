"""Automations triggered by watcher events: PAPER runs at once on the event's stock, cooldowns, LIVE waits for a
confirm on the phone (confirm, decline, expiry)."""

from datetime import datetime, timedelta, timezone

from stocksvest_app import db
from stocksvest_app.alerts.checks import _put
from stocksvest_app.config import Settings
from stocksvest_app.deps import AppContext
from stocksvest_app.watchers import dispatch, store
from test_app_order_automations import (KEY, broker, client, fill_all, market, market_now, new_budget,  # noqa: F401
                                        order_action, run, setup)

SELL_ALL = order_action("SELL", [{"mode": "PERCENT_OF_POSITION", "value": 100}])


def holding_budget(client, setup, broker, seller):
    """A budget holding 4 AAA, 4 BBB and 4 CCC (bought by a scheduled buyer), shared with the event seller."""
    body = {"name": "Buyer", "runTargets": [{"type": "FILTER", "id": setup["filter"]["id"]}],
            "schedules": [{"type": "DAILY", "atTimes": ["10:00"]}],
            "actions": [order_action(sizings=[{"mode": "QUANTITY", "value": 4}])]}
    buyer = client.post("/v1/automations", headers=KEY, json=body).json()
    new_budget(client, setup["portfolio"], 1000, buyer, seller, name="Shared")
    run(client, buyer)
    fill_all(broker)


def event(conn, key, symbol="BBB", severity=4, kind="HOLDING_DROP"):
    return store.add(conn, "holdings-drop", {"key": key, "kind": kind, "symbol": symbol, "severity": severity,
                                             "title": f"{symbol} down", "body": f"{symbol} is down."})


def test_event_automation_runs_on_the_events_stock_then_cools_down(client, setup, broker, market):
    body = {"name": "Drop seller", "eventTrigger": {"kinds": ["HOLDING_DROP"], "minSeverity": 3},
            "actions": [SELL_ALL], "orderGuards": {"regularHoursOnly": False}}
    seller = client.post("/v1/automations", headers=KEY, json=body)
    assert seller.status_code == 201, seller.text
    seller = seller.json()
    assert seller["eventTrigger"]["cooldownMinutes"] == 60 and seller["schedules"] == []
    no_trigger = client.post("/v1/automations", headers=KEY, json={"name": "x", "actions": [SELL_ALL]})
    assert no_trigger.status_code == 422
    holding_budget(client, setup, broker, seller)

    ctx = AppContext(Settings(), market, broker_factory=lambda a: broker)
    conn = db.connect(ctx.settings.db_path)
    low = event(conn, "drop:CCC:low", "CCC", severity=2)  # below minSeverity: no run
    first = event(conn, "drop:BBB:1")
    dispatch.dispatch(ctx, conn)
    assert store.get_event(conn, low)["outcomes"] == [] and store.get_event(conn, low)["status"] == "DONE"
    ev = store.get_event(conn, first)
    [o] = ev["outcomes"]
    assert ev["status"] == "DONE" and o["status"] == "SUCCEEDED", ev
    sells = [x for x in broker.orders.values() if x.side == "SELL"]
    assert [(x.symbol, x.quantity) for x in sells] == [("BBB", 4)]  # only the event's stock, the whole position

    second = event(conn, "drop:AAA:1", "AAA")
    dispatch.dispatch(ctx, conn)
    assert store.get_event(conn, second)["outcomes"][0]["status"] == "COOLDOWN"
    assert client.get(f"/v1/events/{first}", headers=KEY).json()["outcomes"][0]["runId"] == o["runId"]
    trigger = client.get(f"/v1/runs/{o['runId']}", headers=KEY).json()["result"]["trigger"]
    assert (trigger["type"], trigger["eventId"]) == ("EVENT", first)


def test_live_event_run_waits_for_a_confirm(client, setup, broker, market):
    body = {"name": "Live drop seller", "eventTrigger": {"confirmWithinMinutes": 10},
            "actions": [SELL_ALL], "orderGuards": {"regularHoursOnly": False}}
    seller = client.post("/v1/automations", headers=KEY, json=body).json()
    holding_budget(client, setup, broker, seller)
    ctx = AppContext(Settings(), market, broker_factory=lambda a: broker)
    conn = db.connect(ctx.settings.db_path)
    with conn:
        conn.execute("UPDATE broker_accounts SET environment = 'LIVE'")

    first = event(conn, "drop:BBB:1")
    dispatch.dispatch(ctx, conn)
    ev = store.get_event(conn, first)
    assert ev["status"] == "AWAITING_CONFIRM" and not [x for x in broker.orders.values() if x.side == "SELL"]
    r = client.post(f"/v1/events/{first}/confirm", headers=KEY, json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "DONE" and r.json()["outcomes"][0]["status"] == "SUCCEEDED"
    assert [(x.symbol, x.quantity) for x in broker.orders.values() if x.side == "SELL"] == [("BBB", 4)]
    assert client.post(f"/v1/events/{first}/confirm", headers=KEY, json={"confirm": True}).status_code == 409
    assert client.post("/v1/events/evt_nope/confirm", headers=KEY, json={"confirm": True}).status_code == 404

    _put(conn, f"event-run:{seller['id']}", None)  # past the cooldown
    declined = event(conn, "drop:AAA:1", "AAA")
    dispatch.dispatch(ctx, conn)
    assert client.post(f"/v1/events/{declined}/confirm", headers=KEY, json={"confirm": False}).json()["status"] == "DECLINED"

    _put(conn, f"event-run:{seller['id']}", None)
    late = event(conn, "drop:CCC:1", "CCC")
    now = datetime.now(timezone.utc)
    dispatch.dispatch(ctx, conn, now)
    dispatch.dispatch(ctx, conn, now + timedelta(minutes=11))  # not confirmed within 10 minutes
    assert store.get_event(conn, late)["status"] == "EXPIRED"
    assert client.post(f"/v1/events/{late}/confirm", headers=KEY, json={"confirm": True}).status_code == 409
    assert len([x for x in broker.orders.values() if x.side == "SELL"]) == 1  # only the confirmed one
