"""News watcher: held stocks' articles go to the model; roundups and other stocks don't; a quote not in the article
counts as severity 1; a scheduled event tomorrow gets +1; each article is read once; no key is an error."""

import json

from stocksvest_app.watchers import news, store, thread
from test_app_watchers import Market, at, setup

ARTICLES = [
    {"id": 1, "time": "2026-11-02T12:05:00Z", "headline": "BBB Announces Phase 3 Topline Data Date", "summary": "",
     "content": "<p>BBB will report Phase 3 topline data on November 3, 2026 before the market opens.</p>",
     "source": "benzinga", "url": "u1", "symbols": ["BBB"]},
    {"id": 2, "time": "2026-11-02T12:06:00Z", "headline": "12 Health Care Stocks Moving", "summary": "", "content": "x",
     "source": "benzinga", "url": "u2", "symbols": ["BBB", "AAA", "CCC", "DDD", "EEE", "FFF"]},
    {"id": 3, "time": "2026-11-02T12:07:00Z", "headline": "AAA wins award", "summary": "", "content": "x",
     "source": "benzinga", "url": "u3", "symbols": ["AAA"]},
    {"id": 4, "time": "2026-11-02T12:08:00Z", "headline": "BBB CFO interview", "summary": "", "content": "<p>Nothing new.</p>",
     "source": "benzinga", "url": "u4", "symbols": ["BBB"]},
]
MORE = [  # an hour later: a repeat of the same weight (quiet) and a bigger one (pushed)
    {"id": 5, "time": "2026-11-02T13:05:00Z", "headline": "BBB data date reminder", "summary": "",
     "content": "<p>BBB reports data tomorrow.</p>", "source": "benzinga", "url": "u5", "symbols": ["BBB"]},
    {"id": 6, "time": "2026-11-02T13:06:00Z", "headline": "BBB prices share offering", "summary": "",
     "content": "<p>BBB priced an offering of 20 million shares at a discount.</p>", "source": "benzinga", "url": "u6",
     "symbols": ["BBB"]},
]


def answer(kind, severity, quote, event_date=None):
    return json.dumps({"kind": kind, "eventDate": event_date, "dateCertain": bool(event_date), "timing": "BEFORE_OPEN",
                       "direction": "NEGATIVE", "severity": severity, "summary": kind.lower(), "quote": quote})


class FakeModel:
    """Answers chat completions in order; other posts (Expo push) go to `other`."""

    def __init__(self, answers, other):
        self.answers, self.calls, self.other = answers, [], other

    def post(self, url, json=None, headers=None, timeout=None):
        if "chat/completions" not in url:
            return self.other(url, json=json, timeout=timeout)
        self.calls.append(json["messages"][1]["content"])
        content = self.answers[len(self.calls) - 1]
        return type("R", (), {"raise_for_status": lambda s: None,
                              "json": lambda s: {"choices": [{"message": {"content": content}}]}})()


def test_news_watcher_reads_held_stocks_news_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_KEY", "k")
    market = Market("2026-11-02", {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0})
    market.news_items = ARTICLES
    ctx, conn, expo = setup(tmp_path, monkeypatch, market)
    for w in ("holdings-drop", "market", "earnings"):
        store.save_config(conn, thread.WATCHERS[w], enabled=False)
    model = FakeModel([
        json.dumps({"kind": "TRIAL_EVENT_SCHEDULED", "eventDate": "2026-11-03", "dateCertain": True, "timing": "BEFORE_OPEN",
                    "direction": "UNCLEAR", "severity": 3, "summary": "Phase 3 data tomorrow before the open.",
                    "quote": "BBB will report Phase 3 topline data on November 3, 2026 before the market opens."}),
        json.dumps({"kind": "OTHER", "eventDate": None, "dateCertain": False, "timing": "UNKNOWN", "direction": "POSITIVE",
                    "severity": 5, "summary": "Huge.", "quote": "The company doubled its revenue guidance today."}),
    ], expo.post)
    monkeypatch.setattr(news.requests, "post", model.post)  # the same requests module as push

    thread.tick(ctx, conn, at("2026-11-02", "07:10"))
    assert len(model.calls) == 2 and model.calls[0].startswith("Stock: BBB")  # only BBB's own articles (1 and 4)
    [ev] = store.list_events(conn)  # article 4's quote isn't in the text: severity 1, no event
    assert (ev["key"], ev["kind"], ev["severity"]) == ("news:1:BBB", "NEWS_TRIAL_EVENT_SCHEDULED", 4)  # 3 + tomorrow
    assert "before open" in ev["body"] and ev["data"]["answer"]["checked"] and "Core 40" in ev["body"]
    assert expo.sent[-1]["title"].startswith("BBB: BBB Announces")

    thread.tick(ctx, conn, at("2026-11-02", "07:20"))  # same articles again: not read twice
    assert len(model.calls) == 2
    assert market.calls[-1][2] == "2026-11-02T12:08:00Z"  # asks only from the newest article on

    market.news_items = ARTICLES + MORE
    model.answers += [answer("EARNINGS_DATE", 3, "BBB reports data tomorrow.", "2026-11-03"),
                      answer("OFFERING", 2, "BBB priced an offering of 20 million shares at a discount.")]
    thread.tick(ctx, conn, at("2026-11-02", "08:10"))
    assert len(model.calls) == 4
    # the reminder (3 + tomorrow = 4, like the last event) and the offering (2, lifted to 3) stay quiet
    assert [e["key"] for e in store.list_events(conn)] == ["news:1:BBB"]
    assert store.config(conn, news)["state"]["dates"]["BBB"]["date"] == "2026-11-03"  # for the earnings watcher
    model.answers.append(answer("OFFERING", 5, "BBB priced an offering of 20 million shares at a discount."))
    market.news_items = ARTICLES + MORE + [{**MORE[1], "id": 7, "time": "2026-11-02T13:30:00Z"}]
    thread.tick(ctx, conn, at("2026-11-02", "08:30"))
    assert [e["key"] for e in store.list_events(conn)] == ["news:7:BBB", "news:1:BBB"]  # higher severity: through


def test_news_watcher_without_a_key_reports_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("LITELLM_KEY", raising=False)
    ctx, conn, _ = setup(tmp_path, monkeypatch, Market("2026-11-02", {"BBB": 0.0}))
    thread.tick(ctx, conn, at("2026-11-02", "07:10"))
    assert "LITELLM_KEY" in store.config(conn, news)["lastError"]
