"""Email alerts: problems once when they start and once when resolved, failed runs, the daily summary."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fake_market import FakeMarket
from stocksvest_app import db
from stocksvest_app.alerts import checks
from stocksvest_app.alerts.mailer import Mailer
from stocksvest_app.config import Settings
from stocksvest_app.deps import AppContext
from stocksvest_app.runs import store as runs_store

ET = ZoneInfo("America/New_York")


def at(h, m=0, day=20):  # Tuesday 20 Oct 2026, New York time
    return datetime(2026, 10, day, h, m, tzinfo=ET).astimezone(timezone.utc)


class FakeMailer:
    configured = True

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, subject, text):
        if self.fail:
            raise OSError("relay down")
        self.sent.append((subject, text))

    def subjects(self):
        return [s for s, _ in self.sent]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    market = FakeMarket(last_sync=at(10, 55).isoformat())
    mailer = FakeMailer()
    ctx = AppContext(Settings(), market, mailer=mailer)
    conn = db.connect(ctx.settings.db_path)
    db.init(conn)
    yield ctx, conn, market, mailer
    conn.close()


def test_a_problem_is_emailed_once_after_its_grace_and_once_when_resolved(env):
    ctx, conn, market, mailer = env
    t = at(11)
    checks.condition(conn, mailer, "x", "Thing", "broken", t, grace=timedelta(minutes=10))
    assert mailer.sent == []  # within the grace period
    checks.condition(conn, mailer, "x", "Thing", "still broken", t + timedelta(minutes=10), grace=timedelta(minutes=10))
    checks.condition(conn, mailer, "x", "Thing", "still broken", t + timedelta(minutes=15), grace=timedelta(minutes=10))
    assert mailer.subjects() == ["Problem: Thing"] and "still broken" in mailer.sent[0][1]
    assert [p["key"] for p in checks.active(conn)] == ["x"]
    checks.condition(conn, mailer, "x", "Thing", None, t + timedelta(minutes=30))
    assert mailer.subjects() == ["Problem: Thing", "Resolved: Thing"] and "about 30 min" in mailer.sent[1][1]
    assert checks.active(conn) == []


def test_a_blip_shorter_than_the_grace_sends_nothing(env):
    ctx, conn, market, mailer = env
    checks.condition(conn, mailer, "x", "Thing", "broken", at(11), grace=timedelta(minutes=10))
    checks.condition(conn, mailer, "x", "Thing", None, at(11, 5), grace=timedelta(minutes=10))
    assert mailer.sent == [] and checks._get(conn, "alert:problem:x") is None


def test_a_failed_send_is_retried(env):
    ctx, conn, market, mailer = env
    mailer.fail = True
    checks.condition(conn, mailer, "x", "Thing", "broken", at(11))
    mailer.fail = False
    checks.condition(conn, mailer, "x", "Thing", "broken", at(11, 5))
    assert mailer.subjects() == ["Problem: Thing"]


def test_check_runs_at_most_every_five_minutes_and_reports_failed_runs(env):
    ctx, conn, market, mailer = env
    checks.check(ctx, conn, at(11))
    runs_store.add_run(conn, "AUTOMATION", "aut_1", {"name": "Buy"}, 10, result={"actions": []}, error="filters: boom",
                       status="FAILED", retention_days=183)
    checks.check(ctx, conn, at(11, 2))  # too soon: nothing checked
    assert mailer.sent == []
    checks.check(ctx, conn, datetime.now(timezone.utc) + timedelta(minutes=6))  # the run's time is "now"
    failed = [t for subj, t in mailer.sent if subj == "1 automation run(s) failed"]  # (after the close a summary too)
    assert len(failed) == 1 and "Buy: FAILED. filters: boom" in failed[0]


def test_ibkr_down_during_the_session(env, monkeypatch):
    ctx, conn, market, mailer = env
    status = {"lastIncrementalAt": at(10, 55).isoformat(), "lastError": None,
              "liveQuotes": {"enabled": True, "connected": False, "lastError": "10197: competing live session"}}
    monkeypatch.setattr(market, "status", lambda: status)
    checks.check(ctx, conn, at(11))
    status["lastIncrementalAt"] = at(11, 10).isoformat()
    checks.check(ctx, conn, at(11, 11))
    assert mailer.subjects() == ["Problem: Live prices"] and "competing live session" in mailer.sent[0][1]
    status["liveQuotes"]["connected"] = True
    status["lastIncrementalAt"] = at(11, 20).isoformat()
    checks.check(ctx, conn, at(11, 22))
    assert mailer.subjects()[-1] == "Resolved: Live prices"


def test_stale_market_data_only_during_the_session(env):
    ctx, conn, market, mailer = env
    market.last_sync = at(9, 0).isoformat()
    checks.check(ctx, conn, at(8, 0))  # before the open: old data is normal
    assert mailer.sent == []
    checks.check(ctx, conn, at(11))
    assert mailer.subjects() == ["Problem: Market data"]


def test_market_unreachable(env):
    ctx, conn, market, mailer = env
    market.fail = True
    checks.check(ctx, conn, at(11))
    checks.check(ctx, conn, at(11, 11))
    assert mailer.subjects() == ["Problem: Market service"]


def test_broker_differences(env):
    ctx, conn, market, mailer = env
    from stocksvest_app.brokers import store as brokers_store
    from stocksvest_app.orders import store as orders_store
    acct = brokers_store.create_account(conn, {"name": "Paper", "provider": "ALPACA", "environment": "PAPER",
                                               "credentialsRef": "ALPACA"}, "CONNECTED", None)
    orders_store.add_sync(conn, acct["id"], 0, 0, [{"kind": "CASH", "appValue": 95287.24, "brokerValue": 95287.23,
                                                     "detail": "portfolios hold more cash than the broker account"}],
                          None)
    checks.check(ctx, conn, at(11))
    assert mailer.subjects() == ["Problem: Broker account Paper"] and "95287.23" in mailer.sent[0][1]
    orders_store.add_sync(conn, acct["id"], 0, 0, [], None)
    checks.check(ctx, conn, at(11, 6))
    assert mailer.subjects()[-1] == "Resolved: Broker account Paper"


def test_negative_broker_cash(env):
    ctx, conn, market, mailer = env
    from stocksvest_app.brokers import store as brokers_store
    acct = brokers_store.create_account(conn, {"name": "Live", "provider": "ALPACA", "environment": "LIVE",
                                               "credentialsRef": "ALPACA_LIVE"}, "CONNECTED", None)
    brokers_store.set_balances(conn, acct["id"], -0.03, 0, 100)
    checks.check(ctx, conn, at(11))
    assert mailer.subjects() == ["Problem: Negative cash in Live"] and "margin interest" in mailer.sent[0][1]
    brokers_store.set_balances(conn, acct["id"], 5, 5, 100)
    checks.check(ctx, conn, at(11, 6))
    assert mailer.subjects()[-1] == "Resolved: Negative cash in Live"


def test_daily_summary_once_after_the_close(env):
    ctx, conn, market, mailer = env
    market.last_sync = at(16, 5).isoformat()
    checks.check(ctx, conn, at(16, 10))  # before close + 30 min
    assert mailer.sent == []
    checks.check(ctx, conn, at(16, 31))
    checks.check(ctx, conn, at(16, 40))
    assert mailer.subjects() == ["Daily summary 2026-10-20"]
    assert "Automation runs: 0" in mailer.sent[0][1] and "Open problems: none" in mailer.sent[0][1]


def test_off_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    mailer = FakeMailer()
    ctx = AppContext(Settings(), FakeMarket(), mailer=mailer)
    conn = db.connect(ctx.settings.db_path)
    db.init(conn)
    checks.check(ctx, conn, at(11))
    assert mailer.sent == [] and checks._get(conn, "alert:last_check") is None


def test_mailer_needs_relay_login_and_recipient(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert Mailer(Settings()).configured is False
    monkeypatch.setenv("SMTP_HOST", "smtp-relay.brevo.com")
    monkeypatch.setenv("SMTP_USERNAME", "x@smtp-brevo.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("ALERT_EMAIL_TO", "me@example.com")
    s = Settings()
    assert Mailer(s).configured is True and "secret" not in repr(s)


def test_holdings_no_longer_halal_are_emailed_daily(env, monkeypatch):
    ctx, conn, market, mailer = env
    from decimal import Decimal
    from stocksvest_app.orders import halal_exit
    held = [{"portfolio": {"name": "P"}, "budgetId": None, "symbol": "AAA", "quantity": Decimal(2),
             "reason": "NON_COMPLIANT", "removedAt": "2026-10-19T06:00:00+00:00"},
            {"portfolio": {"name": "P"}, "budgetId": None, "symbol": "BBB", "quantity": Decimal(1),
             "reason": "PENDING", "removedAt": "2026-10-19T06:00:00+00:00"}]
    monkeypatch.setattr(halal_exit, "held_removed", lambda ctx, conn: held)
    checks.check(ctx, conn, at(11))
    checks.check(ctx, conn, at(12))  # once a day
    halal = [t for s, t in mailer.sent if "no longer halal" in s]
    assert len(halal) == 1 and "2 AAA, NON_COMPLIANT" in halal[0] and "Sold at market" in halal[0]
    assert "BBB, PENDING" in halal[0] and "sell by hand" in halal[0]
    checks.check(ctx, conn, at(11, day=21))  # and again the next day while still held
    assert len([s for s in mailer.subjects() if "no longer halal" in s]) == 2
