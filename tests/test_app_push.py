"""Push notifications: the phone registers, categories switch off, events and email alerts reach Expo."""

from fastapi.testclient import TestClient

from fake_market import FakeMarket
from stocksvest_app import db
from stocksvest_app.alerts import checks, push
from stocksvest_app.api import create_app
from stocksvest_app.audit import store as audit
from stocksvest_app.config import Settings

KEY = {"X-API-Key": "app-key"}
TOKEN = "ExponentPushToken[abc]"


class FakeExpo:
    def __init__(self):
        self.sent = []
        self.error = None

    def post(self, url, json, timeout):
        self.sent += json
        tickets = [{"status": "error", "details": {"error": self.error}} if self.error else {"status": "ok"} for _ in json]
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"data": tickets}})()


def test_register_switch_off_and_push_events_and_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_KEYS", "app-key")
    monkeypatch.setenv("APP_RUN_SCHEDULER", "false")
    expo = FakeExpo()
    monkeypatch.setattr(push.requests, "post", expo.post)
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket())) as c:
        assert c.post("/v1/push/devices", json={"token": "nope"}, headers=KEY).status_code == 422
        assert c.post("/v1/push/devices", json={"token": TOKEN}, headers=KEY).json()["devices"] == 1
        assert c.post("/v1/push/devices", json={"token": TOKEN}, headers=KEY).json()["devices"] == 1  # again: no duplicate
        assert c.put("/v1/push/categories", json={"bogus": False}, headers=KEY).status_code == 422
        cats = c.put("/v1/push/categories", json={"money": False}, headers=KEY).json()["categories"]
        assert {x["key"]: x["enabled"] for x in cats}["money"] is False
        cats = c.put("/v1/push/categories", json={"fills": True}, headers=KEY).json()["categories"]
        assert {x["key"]: x["enabled"] for x in cats}["money"] is False  # not named: stays off
        assert c.post("/v1/push/test", headers=KEY).json() == {"sent": 1}

    conn = db.connect(Settings().db_path)
    expo.sent.clear()
    audit.record(conn, "FILL_IMPORTED", "ORDER", "o1", details={"symbol": "SHLS", "side": "BUY", "quantity": "1.25",
                                                               "price": "7.70"})
    push.events(conn)
    assert expo.sent == []  # the first call only sets the cursor: history is never pushed
    audit.record(conn, "FILL_IMPORTED", "ORDER", "o2", details={"symbol": "SHLS", "side": "SELL", "quantity": "1.25",
                                                               "price": "7.74"})
    audit.record(conn, "DIVIDEND", "BROKER_ACCOUNT", "a1", details={"symbol": "SHLS", "amount": "0.10"})  # money is off
    audit.record(conn, "ORDER_PLACED", "ORDER", "o3")  # not an event
    push.events(conn)
    push.events(conn)  # nothing new: nothing repeated
    assert [(m["title"], m["data"]["category"]) for m in expo.sent] == [("Sold 1.25 SHLS", "fills")]

    expo.sent.clear()
    mailer = type("M", (), {"send": lambda s, subject, text: None})()
    checks._send(conn, mailer, "Kill rule: x", "email only")  # no category: email only
    checks._send(conn, mailer, "Daily summary", "a\n\nb", "summary")
    assert [(m["title"], m["body"]) for m in expo.sent] == [("Daily summary", "a b")]

    expo.error = "DeviceNotRegistered"  # the app was removed from the phone
    assert push.send(conn, "test", "t", "b") == 0 and push.tokens(conn) == []
    conn.close()
