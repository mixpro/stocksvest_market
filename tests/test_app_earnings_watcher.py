"""Earnings watcher: an announced date fires the day before (higher after a run-up), once; without one the 8-K
estimate fires 3 days ahead, one severity lower; an estimate near an announced date stays quiet."""

from stocksvest_app.watchers import news, store, thread
from test_app_watchers import Market, at, setup


class EarningsMarket(Market):
    def __init__(self, run_up, days_to_earnings):
        super().__init__("2026-11-02", {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0})
        self.run_up, self.dte = run_up, days_to_earnings

    def indicators(self, symbols=None):
        return {"indicators": {t: {"daysToEarnings": self.dte} for t in symbols}}

    def price_change(self, period_amount, period_unit, symbols=None):
        self.calls.append(("price_change", period_amount, period_unit, symbols))
        return {"changes": {t: {"change": 1.0, "changePercent": self.run_up} for t in symbols}}


def only_earnings(conn):
    for w in ("holdings-drop", "market", "news"):
        store.save_config(conn, thread.WATCHERS[w], enabled=False)


def test_announced_date_fires_the_day_before_once(tmp_path, monkeypatch):
    market = EarningsMarket(run_up=25.0, days_to_earnings=40)
    ctx, conn, expo = setup(tmp_path, monkeypatch, market)
    only_earnings(conn)
    store.save_config(conn, news, state={"dates": {"BBB": {"date": "2026-11-03", "timing": "AFTER_CLOSE",
                                                            "articleId": 9}}})

    thread.tick(ctx, conn, at("2026-11-02", "18:00"))
    [ev] = store.list_events(conn)
    assert (ev["key"], ev["kind"], ev["severity"]) == ("earnings:BBB:2026-11-03", "EARNINGS_SOON", 4)  # run-up 25%
    assert ev["title"] == "BBB reports tomorrow after the close"
    assert "up 25% in the past month" in ev["body"] and "13-16%" in ev["body"] and "Core 40" in ev["body"]
    assert ("price_change", 1, "MONTHS", ["BBB"]) in market.calls
    assert expo.sent[-1]["title"] == ev["title"]

    thread.tick(ctx, conn, at("2026-11-03", "09:00"))  # report day: same key, nothing new
    assert len(store.list_events(conn)) == 1


def test_estimate_fires_three_days_ahead_unless_a_date_was_announced(tmp_path, monkeypatch):
    market = EarningsMarket(run_up=5.0, days_to_earnings=3)
    ctx, conn, _ = setup(tmp_path, monkeypatch, market)
    only_earnings(conn)

    thread.tick(ctx, conn, at("2026-11-02", "08:00"))
    [ev] = store.list_events(conn)
    assert (ev["key"], ev["severity"]) == ("earnings:BBB:2026-11-05", 2) and ev["data"]["estimated"]
    assert ev["title"] == "BBB reports (estimated) around Nov 5" and "+91 days" in ev["body"]
    assert "13-16%" not in ev["body"]  # no run-up

    market.dte = 2  # next day: the same estimated date, not again
    thread.tick(ctx, conn, at("2026-11-03", "08:00"))
    assert len(store.list_events(conn)) == 1

    store.save_config(conn, news, state={"dates": {"BBB": {"date": "2026-11-12", "timing": "BEFORE_OPEN"}}})
    market.dte = -3  # an estimate days before the announced date: quiet
    thread.tick(ctx, conn, at("2026-11-04", "08:00"))
    assert len(store.list_events(conn)) == 1
