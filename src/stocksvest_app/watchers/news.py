"""Watcher: news about a held stock, read by the news model (WATCH-8). Design: UML/NewsReading.mmd.

Every 2 minutes, day and night: new Alpaca news for held stocks (market GET /v1/news, press releases within minutes).
Market roundups (more than 5 symbols, e.g. "12 Health Care Stocks Moving") are skipped. The model (NEWS_MODEL through
LiteLLM) answers in a fixed schema: kind, date, date certainty, timing, direction, severity and a quote. The quote must
appear word for word in the article, else the answer counts as severity 1. Code, not the model, sets the final
severity: at least 3 for news that moves stocks hard either way (trial data, FDA decision, results, share offering;
the model rated IOVA's 2026-10-29 lung data "2, unclear" and the stock fell 35%), and +1 for a scheduled event
(results date, trial readout, FDA decision) today or tomorrow. An event is stored at
minSeverity or above, with the article text and the model's answer. After one, the same stock gets another within
repeatHours only for a higher severity (Intel's results evening had about 20 articles at 3 or more).
"""

import html
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import store

NAME = "news"
EVERY = timedelta(minutes=2)
HOURS = "ALWAYS"  # releases come at 7am, after 4pm and late at night too
ENABLED = True
DEFAULTS = {"minSeverity": 3.0, "repeatHours": 6.0}
ET = ZoneInfo("America/New_York")

KINDS = ("TRIAL_DATA", "TRIAL_EVENT_SCHEDULED", "EARNINGS_RESULTS", "EARNINGS_DATE", "FDA_DECISION", "OFFERING",
         "GUIDANCE", "MERGER_ACQUISITION", "LEGAL_REGULATORY", "ANALYST", "OTHER")
SCHEDULED = {"TRIAL_EVENT_SCHEDULED", "EARNINGS_DATE", "FDA_DECISION"}
BIG_MOVERS = {"TRIAL_DATA", "FDA_DECISION", "EARNINGS_RESULTS", "OFFERING"}
TIMINGS = ("BEFORE_OPEN", "DURING_SESSION", "AFTER_CLOSE", "UNKNOWN")
DIRECTIONS = ("POSITIVE", "NEGATIVE", "MIXED", "UNCLEAR")
MAX_TEXT = 12000
PROMPT = f"""You read one news article about a US stock for a long-only investor who holds it.
Answer with one JSON object and nothing else:
{{"kind": one of {list(KINDS)},
 "eventDate": "YYYY-MM-DD" of the event the article is about (the readout, results, decision), or null,
 "dateCertain": true if the article states that date, false if estimated or missing,
 "timing": one of {list(TIMINGS)} (when the event is or was released, New York time),
 "direction": one of {list(DIRECTIONS)} for the stock price,
 "severity": 1-5, how much this could move the stock in a day (1 routine, 3 notable, 5 could move it 20%+),
 "quote": one sentence copied exactly, word for word, from the article that supports your answer,
 "summary": one short sentence for a phone notification}}"""


def plain(article: dict) -> str:
    """Headline, summary and full text without HTML, at most MAX_TEXT characters."""
    body = re.sub(r"<[^>]+>", " ", article.get("content") or "")
    text = "\n".join(x for x in (article["headline"], article.get("summary"), html.unescape(body)) if x)
    return re.sub(r"[ \t\r\f\v]+", " ", text)[:MAX_TEXT]


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s).strip().lower()


def ask(settings, symbol: str, text: str) -> dict:
    """The model's answer for one article, checked: an unreadable answer, an unknown value or a quote that isn't in the
    text comes back with severity 1 and `checked: false`.

    Raises:
        requests.RequestException: LiteLLM unreachable or refusing (the pass is retried).
    """
    r = requests.post(f"{settings.litellm_url}/v1/chat/completions", timeout=60,
                      headers={"Authorization": f"Bearer {settings.litellm_key}"},
                      # max_tokens: haiku-news reasons before answering (~500 tokens in all)
                      json={"model": settings.news_model, "max_tokens": 2000, "temperature": 0,
                            "messages": [{"role": "system", "content": PROMPT},
                                         {"role": "user", "content": f"Stock: {symbol}\n\n{text}"}]})
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"] or ""
    try:
        a = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        ok = (a.get("kind") in KINDS and a.get("timing") in TIMINGS and a.get("direction") in DIRECTIONS
              and isinstance(a.get("severity"), int) and 1 <= a["severity"] <= 5
              and len(_norm(a.get("quote") or "")) >= 20 and _norm(a["quote"]) in _norm(text))
        if a.get("eventDate"):
            date.fromisoformat(a["eventDate"])
    except (ValueError, TypeError, AttributeError):
        a, ok = {"kind": "OTHER", "raw": raw[:2000]}, False
    return {**a, "checked": ok, "severity": a["severity"] if ok else 1}


def severity(answer: dict, now: datetime) -> int:
    """The final severity: the model's, at least 3 for BIG_MOVERS, +1 for a scheduled event today or tomorrow (New York)."""
    sev = answer["severity"]
    if answer["checked"] and answer["kind"] in BIG_MOVERS:
        sev = max(sev, 3)
    if answer["checked"] and answer["kind"] in SCHEDULED and answer.get("eventDate"):
        days = (date.fromisoformat(answer["eventDate"]) - now.astimezone(ET).date()).days
        sev += 0 <= days <= 1
    return min(5, sev)


def check(ctx, conn, now, settings, state) -> list[dict]:
    if not ctx.settings.litellm_key:
        raise RuntimeError("LITELLM_KEY is not set: news is not read")
    held = store.held(conn)
    symbols = sorted(set(held) & ctx.market.universe().keys())  # the market service 404s on unknown symbols
    if not symbols:
        return []
    since = state.get("since") or (now - timedelta(hours=1)).isoformat()
    articles, token = [], None
    for _ in range(4):  # ponytail: at most 200 articles a pass; a longer outage only reads the newest 200
        page = ctx.market_api.news(symbols, since, None, 50, token, True)
        articles += page["news"]
        token = page.get("nextPageToken")
        if not token:
            break
    seen = set(state.get("seen", []))
    out = []
    for n in sorted(articles, key=lambda n: n["time"]):
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        mine = [s for s in n["symbols"] if s in held]
        if not mine or len(n["symbols"]) > 5:
            continue
        text = plain(n)
        for sym in mine[:2]:  # ponytail: a merger names both sides; more than 2 held symbols is a roundup
            a = ask(ctx.settings, sym, text)
            sev = severity(a, now)
            last = state.setdefault("last", {}).get(sym)
            if sev < settings["minSeverity"] or (
                    last and sev <= last["severity"]
                    and now - datetime.fromisoformat(last["at"]) < timedelta(hours=settings["repeatHours"])):
                continue
            state["last"][sym] = {"at": now.isoformat(), "severity": sev}
            quote = f' "{a["quote"]}"' if a["checked"] else ""
            out.append({
                "key": f"news:{n['id']}:{sym}", "kind": f"NEWS_{a['kind']}", "symbol": sym, "severity": sev,
                "title": f"{sym}: {n['headline'][:100]}",
                "body": f"{a.get('summary') or n['headline']} ({a.get('direction', 'UNCLEAR').lower()}, "
                        f"{a.get('timing', 'UNKNOWN').lower().replace('_', ' ')}){quote} Held in {', '.join(held[sym])}.",
                "data": {"articleId": n["id"], "url": n.get("url"), "source": n.get("source"), "publishedAt": n["time"],
                         "answer": a, "text": text},
            })
    if articles:
        state["since"] = max(n["time"] for n in articles)
    state["seen"] = sorted(seen)[-500:]
    return out
