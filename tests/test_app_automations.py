from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from fake_market import FakeMarket
from stocksvest_app import db as app_db
from stocksvest_app.api import create_app
from stocksvest_app.automations import runner, store
from stocksvest_app.automations.models import Schedule
from stocksvest_app.automations.scheduler import tick
from stocksvest_app.automations.timing import next_fires
from stocksvest_app.config import Settings
from stocksvest_app.deps import AppContext
from stocksvest_app.runs import store as runs_store

KEY = {"X-API-Key": "app-key"}
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
SCHEDULE = TypeAdapter(Schedule)
CREATED = datetime(2026, 10, 1, tzinfo=UTC)


def cal(start, end):
    return FakeMarket().calendar(str(start), str(end))["days"]


def fires(raw: dict, after: datetime, n: int = 5, fired: int = 0) -> list[str]:
    s = SCHEDULE.validate_python(raw)
    return [t.astimezone(ET).strftime("%a %Y-%m-%d %H:%M") for t in next_fires(s, after, CREATED, cal, n, fired)]


def et(*args) -> datetime:
    return datetime(*args, tzinfo=ET)


# ------------------------------------------------------------------ schedule timing


def test_daily_with_exceptions():
    raw = {"type": "DAILY", "atTimes": ["16:30", "09:45"], "exceptions": ["2026-10-20"]}
    assert fires(raw, et(2026, 10, 19, 10, 0), 4) == [
        "Mon 2026-10-19 16:30", "Wed 2026-10-21 09:45", "Wed 2026-10-21 16:30", "Thu 2026-10-22 09:45"]


def test_daily_in_another_time_zone():
    raw = {"type": "DAILY", "atTimes": ["18:00"], "timezone": "Asia/Jerusalem"}
    got = SCHEDULE.validate_python(raw)
    t = next_fires(got, datetime(2026, 10, 19, 12, 0, tzinfo=UTC), CREATED, cal)[0]
    assert t.astimezone(ZoneInfo("Asia/Jerusalem")).hour == 18


def test_weekly_every_two_weeks():
    raw = {"type": "WEEKLY", "everyNWeeks": 2, "onDays": ["MON", "THU"], "atTimes": ["10:00"],
           "startAt": "2026-10-05T00:00:00-04:00"}
    assert fires(raw, et(2026, 10, 5, 0, 0), 4) == [
        "Mon 2026-10-05 10:00", "Thu 2026-10-08 10:00", "Mon 2026-10-19 10:00", "Thu 2026-10-22 10:00"]


def test_interval_minutes_inside_window_on_weekdays():
    raw = {"type": "INTERVAL", "every": 30, "unit": "MINUTE", "onDays": ["MON", "TUE", "WED", "THU", "FRI"],
           "window": {"start": "09:30", "end": "10:30"}, "startAt": "2026-10-16T09:00:00-04:00"}
    assert fires(raw, et(2026, 10, 16, 10, 15), 4) == [  # Friday, then the weekend is skipped
        "Fri 2026-10-16 10:30", "Mon 2026-10-19 09:30", "Mon 2026-10-19 10:00", "Mon 2026-10-19 10:30"]


def test_interval_days_keep_wall_clock_across_dst():
    raw = {"type": "INTERVAL", "every": 1, "unit": "DAY", "startAt": "2026-10-30T09:30:00-04:00"}
    assert fires(raw, et(2026, 10, 30, 10, 0), 3) == [  # US DST ends Sun Nov 1
        "Sat 2026-10-31 09:30", "Sun 2026-11-01 09:30", "Mon 2026-11-02 09:30"]


def test_once_and_end_conditions():
    once = {"type": "ONCE", "at": "2026-10-20T09:40:00-04:00"}
    assert fires(once, et(2026, 10, 19), 3) == ["Tue 2026-10-20 09:40"]
    assert fires(once, et(2026, 10, 21), 3) == []
    until = {"type": "DAILY", "atTimes": ["12:00"], "end": {"type": "UNTIL", "until": "2026-10-21T23:00:00-04:00"}}
    assert fires(until, et(2026, 10, 19), 5) == ["Mon 2026-10-19 12:00", "Tue 2026-10-20 12:00", "Wed 2026-10-21 12:00"]
    count = {"type": "DAILY", "atTimes": ["12:00"], "end": {"type": "COUNT", "count": 3}}
    assert len(fires(count, et(2026, 10, 19), 5, fired=2)) == 1


def test_market_schedule_follows_the_trading_calendar():
    raw = {"type": "MARKET", "anchor": "REGULAR_OPEN", "offsetMinutes": 30, "repeatEveryMinutes": 120}
    # Wed Nov 25: 10:00, 12:00, 14:00 (16:00 close). Thanksgiving closed. Fri early close 13:00: 10:00, 12:00.
    assert fires(raw, et(2026, 11, 25, 9, 0), 6) == [
        "Wed 2026-11-25 10:00", "Wed 2026-11-25 12:00", "Wed 2026-11-25 14:00",
        "Fri 2026-11-27 10:00", "Fri 2026-11-27 12:00", "Mon 2026-11-30 10:00"]
    before_close = {"type": "MARKET", "anchor": "REGULAR_CLOSE", "offsetMinutes": -15}
    assert fires(before_close, et(2026, 11, 27, 9, 0), 2) == ["Fri 2026-11-27 12:45", "Mon 2026-11-30 15:45"]


def test_schedule_validation():
    for bad in ({"type": "INTERVAL", "every": 0, "unit": "MINUTE"},
                {"type": "DAILY", "atTimes": ["10:00"], "timezone": "Mars/Base"},
                {"type": "ONCE", "at": "2026-10-20T09:40:00"},
                {"type": "MARKET", "anchor": "REGULAR_OPEN", "repeatUntil": "REGULAR_CLOSE"},
                {"type": "DAILY", "atTimes": ["10:00"], "end": {"type": "COUNT"}}):
        with pytest.raises(ValueError):
            SCHEDULE.validate_python(bad)


# ------------------------------------------------------------------ app fixtures


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    return Settings()


@pytest.fixture
def market():
    return FakeMarket(last_sync=datetime.now(UTC).isoformat())


@pytest.fixture
def client(settings, market):
    with TestClient(create_app(settings, market_factory=lambda s: market)) as c:
        yield c


@pytest.fixture
def tech_filter(client):
    return client.post("/v1/filters", headers=KEY, json={
        "name": "Tech", "inputs": [{"type": "UNIVERSE"}],
        "actions": [{"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}]}).json()


def automation_body(filter_id, schedules=None, actions=None, name="Tech alert"):
    return {
        "name": name,
        "runTargets": [{"type": "FILTER", "id": filter_id}],
        "schedules": schedules or [{"type": "DAILY", "atTimes": ["10:00"]}],
        "actions": actions or [{"type": "NOTIFY", "title": "{name}: {count} matches", "message": "Daily scan"}],
    }


# ------------------------------------------------------------------ API


def test_crud_versions_and_events(client, tech_filter):
    r = client.post("/v1/automations", headers=KEY, json=automation_body(tech_filter["id"]))
    assert r.status_code == 201
    a = r.json()
    assert a["status"] == "ACTIVE" and a["configVersion"] == 1 and a["schedules"][0]["id"].startswith("sch_")

    body = automation_body(tech_filter["id"], name="Renamed")
    body["schedules"][0]["id"] = a["schedules"][0]["id"]  # keeps its id
    updated = client.put(f"/v1/automations/{a['id']}", headers=KEY, json=body).json()
    assert updated["configVersion"] == 2 and updated["schedules"][0]["id"] == a["schedules"][0]["id"]

    assert client.post(f"/v1/automations/{a['id']}/pause", headers=KEY).json()["status"] == "PAUSED"
    assert client.post(f"/v1/automations/{a['id']}/resume", headers=KEY).json()["status"] == "ACTIVE"
    events = client.get(f"/v1/automations/{a['id']}/events", headers=KEY).json()["events"]
    assert [e["type"] for e in events] == ["RESUMED", "PAUSED", "UPDATED", "CREATED"]
    assert events[2]["changes"]["name"] == {"from": "Tech alert", "to": "Renamed"}

    assert client.delete(f"/v1/filters/{tech_filter['id']}", headers=KEY).status_code == 409  # used by automation
    assert client.delete(f"/v1/automations/{a['id']}", headers=KEY).status_code == 204
    assert client.get(f"/v1/automations/{a['id']}", headers=KEY).status_code == 404


def test_validation(client, tech_filter):
    bad_target = automation_body("flt_missing")
    assert client.post("/v1/automations", headers=KEY, json=bad_target).status_code == 422
    order = automation_body(tech_filter["id"], actions=[{"type": "ADD_TO_WATCHLIST"}])
    r = client.post("/v1/automations", headers=KEY, json=order)
    assert r.status_code == 422 and "not available yet" in r.text
    for schedule in ({"type": "INTERVAL", "every": 1, "unit": "MINUTE"},
                     {"type": "MARKET", "anchor": "REGULAR_OPEN", "repeatEveryMinutes": 1}):
        r = client.post("/v1/automations", headers=KEY, json=automation_body(tech_filter["id"], schedules=[schedule]))
        assert r.status_code == 201, r.text  # every minute is allowed
    for schedule in ({"type": "INTERVAL", "every": 0, "unit": "MINUTE"},
                     {"type": "MARKET", "anchor": "REGULAR_OPEN", "repeatEveryMinutes": 0}):
        r = client.post("/v1/automations", headers=KEY, json=automation_body(tech_filter["id"], schedules=[schedule]))
        assert r.status_code == 422


def test_preview_and_run_now(client, tech_filter):
    a = client.post("/v1/automations", headers=KEY, json=automation_body(tech_filter["id"])).json()

    preview = client.post(f"/v1/automations/{a['id']}/preview", headers=KEY).json()
    assert preview["preview"] is True and preview["status"] == "SUCCEEDED"
    assert preview["result"]["actions"][0]["wouldNotify"] == {
        "title": "Tech alert: 2 matches", "message": "Daily scan", "symbols": ["AAA", "CCC"]}
    assert client.get("/v1/notifications", headers=KEY).json()["unread"] == 0  # nothing sent

    run = client.post(f"/v1/automations/{a['id']}/run", headers=KEY).json()
    assert run["kind"] == "AUTOMATION" and run["status"] == "SUCCEEDED"
    res = run["result"]
    assert res["trigger"]["type"] == "MANUAL" and res["configVersion"] == 1
    assert res["targets"][0]["steps"] == [{"step": "INPUTS", "count": 4}, {"step": "SECTOR", "count": 2}]
    assert [m["ticker"] for m in res["matched"]] == ["AAA", "CCC"] and res["matched"][0]["price"] == 10.0
    assert res["market"]["dataAgeMinutes"] == 0
    assert res["metrics"]["matchedCount"] == 2

    inbox = client.get("/v1/notifications", headers=KEY).json()
    assert inbox["unread"] == 1
    n = inbox["notifications"][0]
    assert n["symbols"] == ["AAA", "CCC"] and n["runId"] == run["id"]
    assert client.post(f"/v1/notifications/{n['id']}/read", headers=KEY).json() == {"marked": 1}
    assert client.get("/v1/notifications?unreadOnly=true", headers=KEY).json()["notifications"] == []
    assert [r["id"] for r in client.get(f"/v1/automations/{a['id']}/runs", headers=KEY).json()["runs"]] == [run["id"]]


def test_unsaved_preview_and_schedule_preview(client, tech_filter):
    out = client.post("/v1/automations/preview", headers=KEY, json=automation_body(tech_filter["id"])).json()
    assert out["result"]["metrics"]["matchedCount"] == 2
    times = client.post("/v1/automations/schedule-preview?count=3", headers=KEY,
                        json={"schedules": [{"type": "DAILY", "atTimes": ["10:00"]}]}).json()
    assert len(times["schedules"][0]["next"]) == 3


def test_skip_when_no_matches_and_failed_filter(client, tech_filter):
    empty = client.post("/v1/filters", headers=KEY, json={
        "name": "None", "inputs": [{"type": "UNIVERSE"}],
        "actions": [{"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "MORE_THAN", "value": 1e9}]}).json()
    a = client.post("/v1/automations", headers=KEY, json=automation_body(
        empty["id"], actions=[{"type": "NOTIFY", "title": "x", "skipWhenNoMatches": True}])).json()
    run = client.post(f"/v1/automations/{a['id']}/run", headers=KEY).json()
    assert run["status"] == "SUCCEEDED" and run["result"]["actions"][0]["status"] == "SKIPPED"
    assert client.get("/v1/notifications", headers=KEY).json()["unread"] == 0


def test_switch_and_next_runs(client, tech_filter):
    a = client.post("/v1/automations", headers=KEY, json=automation_body(tech_filter["id"])).json()
    assert len(client.get(f"/v1/automations/{a['id']}/next-runs", headers=KEY).json()["schedules"][0]["next"]) == 5
    sw = client.get("/v1/automations/switch", headers=KEY).json()
    assert sw["automationsEnabled"] is True and sw["scheduler"]["running"] is False
    off = client.put("/v1/automations/switch", headers=KEY, json={"automationsEnabled": False, "reason": "testing"}).json()
    assert off["automationsEnabled"] is False and off["reason"] == "testing"
    client.post(f"/v1/automations/{a['id']}/pause", headers=KEY)
    assert client.get(f"/v1/automations/{a['id']}/next-runs", headers=KEY).json()["schedules"] == []


# ------------------------------------------------------------------ scheduler and runner


@pytest.fixture
def env(settings, market):
    ctx = AppContext(settings, market)
    conn = app_db.connect(settings.db_path)
    app_db.init(conn)
    from stocksvest_app.filters import store as filters_store
    f = filters_store.save_filter(conn, {"name": "Tech", "description": "", "isActive": True,
                                         "inputs": [{"type": "UNIVERSE"}],
                                         "actions": [{"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}]})
    yield ctx, conn, f["id"]
    conn.close()


def make(conn, filter_id, schedule: dict) -> dict:
    data = automation_body(filter_id, schedules=[{**schedule, "id": "sch_1"}])
    data["description"] = ""
    return store.create_automation(conn, data)


def runs_of(conn, aid) -> list[dict]:
    return runs_store.list_runs(conn, "AUTOMATION", aid, limit=100, include_result=True)


def test_scheduler_fires_once_per_time(env):
    ctx, conn, fid = env
    a = make(conn, fid, {"type": "DAILY", "atTimes": ["10:00"]})
    assert tick(ctx, conn, et(2026, 10, 19, 9, 0)) == []  # first pass only plans
    assert store.get_state(conn, "sch_1")["next_fire_at"].startswith("2026-10-19T14:00")
    assert tick(ctx, conn, et(2026, 10, 19, 9, 59)) == []
    runs = tick(ctx, conn, et(2026, 10, 19, 10, 0, 20))
    assert len(runs) == 1 and runs[0]["result"]["trigger"]["scheduledFor"].startswith("2026-10-19T14:00")
    assert tick(ctx, conn, et(2026, 10, 19, 10, 1)) == []  # not again
    assert store.get_state(conn, "sch_1")["next_fire_at"].startswith("2026-10-20T14:00")
    # Even if the state were lost, the fire record stops a second run of the same time.
    store.set_state(conn, a["id"], "sch_1", "2026-10-19T13:00:00+00:00", "2026-10-19T14:00:00+00:00")
    assert tick(ctx, conn, et(2026, 10, 19, 10, 2)) == []
    assert len(runs_of(conn, a["id"])) == 1


@pytest.mark.parametrize("policy, expected", [
    ("SKIP", [("SKIPPED", "MISSED")]),
    ("RUN_ONCE", [("SUCCEEDED", None)]),
    ("RUN_ALL", [("SUCCEEDED", None)] * 3),
])
def test_missed_policies(env, policy, expected):
    ctx, conn, fid = env
    a = make(conn, fid, {"type": "DAILY", "atTimes": ["10:00"], "missedPolicy": policy})
    tick(ctx, conn, et(2026, 10, 19, 9, 0))
    tick(ctx, conn, et(2026, 10, 21, 11, 0))  # down for Mon, Tue, Wed 10:00
    got = [(r["status"], r["result"]["skipReason"]) for r in runs_of(conn, a["id"])]
    assert got == expected
    if policy == "SKIP":
        assert runs_of(conn, a["id"])[0]["result"]["missedCount"] == 3
    assert store.get_state(conn, "sch_1")["next_fire_at"].startswith("2026-10-22T14:00")


def test_count_end_condition(env):
    ctx, conn, fid = env
    a = make(conn, fid, {"type": "DAILY", "atTimes": ["10:00"], "end": {"type": "COUNT", "count": 2}})
    tick(ctx, conn, et(2026, 10, 19, 9, 0))
    for day in (19, 20, 21):
        tick(ctx, conn, et(2026, 10, day, 10, 0, 30))
    assert len(runs_of(conn, a["id"])) == 2
    assert store.get_state(conn, "sch_1")["next_fire_at"] is None


def test_switch_off_records_skips_and_paused_does_not_fire(env):
    ctx, conn, fid = env
    a = make(conn, fid, {"type": "DAILY", "atTimes": ["10:00"]})
    tick(ctx, conn, et(2026, 10, 19, 9, 0))
    store.set_switch(conn, False, "maintenance")
    tick(ctx, conn, et(2026, 10, 19, 10, 0, 30))
    assert [(r["status"], r["result"]["skipReason"]) for r in runs_of(conn, a["id"])] == [("SKIPPED", "SWITCH_OFF")]
    assert store.unread_count(conn) == 0

    store.set_switch(conn, True, None)
    store.set_status(conn, a["id"], "PAUSED")
    tick(ctx, conn, et(2026, 10, 20, 10, 0, 30))
    assert len(runs_of(conn, a["id"])) == 1
    # Resuming starts from now: Tuesday's missed time is not replayed.
    store.set_status(conn, a["id"], "ACTIVE")
    assert tick(ctx, conn, et(2026, 10, 20, 11, 0)) == []
    assert store.get_state(conn, "sch_1")["next_fire_at"].startswith("2026-10-21T14:00")


def test_market_schedule_runs_on_trading_days(env):
    ctx, conn, fid = env
    a = make(conn, fid, {"type": "MARKET", "anchor": "REGULAR_OPEN", "offsetMinutes": 20})
    tick(ctx, conn, et(2026, 11, 25, 8, 0))
    tick(ctx, conn, et(2026, 11, 25, 9, 50, 10))
    tick(ctx, conn, et(2026, 11, 26, 9, 50, 10))  # Thanksgiving: nothing due
    assert len(runs_of(conn, a["id"])) == 1
    assert store.get_state(conn, "sch_1")["next_fire_at"].startswith("2026-11-27T14:50")


def test_overlapping_runs_are_skipped(env):
    ctx, conn, fid = env
    a = make(conn, fid, {"type": "DAILY", "atTimes": ["10:00"]})
    lock = runner._lock_for(a["id"])
    lock.acquire()
    try:
        run = runner.run_automation(ctx, conn, a, "MANUAL")
    finally:
        lock.release()
    assert run["status"] == "SKIPPED" and run["result"]["skipReason"] == "OVERLAP"


def test_runner_market_down(settings):
    ctx = AppContext(settings, FakeMarket(fail=True))
    conn = app_db.connect(settings.db_path)
    app_db.init(conn)
    from stocksvest_app.filters import store as filters_store
    f = filters_store.save_filter(conn, {"name": "All", "description": "", "isActive": True,
                                         "inputs": [{"type": "UNIVERSE"}], "actions": []})
    a = make(conn, f["id"], {"type": "DAILY", "atTimes": ["10:00"]})
    run = runner.run_automation(ctx, conn, a, "MANUAL")
    assert run["status"] == "FAILED" and "unreachable" in run["error"]
    assert store.unread_count(conn) == 0


def test_next_run_and_last_run_in_listing(client, tech_filter):
    a = client.post("/v1/automations", headers=KEY, json=automation_body(tech_filter["id"])).json()
    listed = client.get("/v1/automations", headers=KEY).json()["automations"][0]
    assert listed["nextRunAt"] is not None and listed["lastRun"] is None
    nxt = datetime.fromisoformat(listed["nextRunAt"])
    assert nxt > datetime.now(UTC) and nxt.astimezone(ET).strftime("%H:%M") == "10:00"

    run = client.post(f"/v1/automations/{a['id']}/run", headers=KEY).json()
    got = client.get(f"/v1/automations/{a['id']}", headers=KEY).json()
    assert got["lastRun"]["id"] == run["id"] and got["lastRun"]["status"] == "SUCCEEDED"

    client.post(f"/v1/automations/{a['id']}/pause", headers=KEY)
    assert client.get(f"/v1/automations/{a['id']}", headers=KEY).json()["nextRunAt"] is None
