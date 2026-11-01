"""WATCH-11 step 2: replay a sample of stock-days through the news watcher's model reading (watchers/news.py).

Runs inside the app container on tinkergw (the only place with LITELLM_KEY):

    docker cp sample.json stocksvest-app:/tmp/ && docker cp scripts/replay_news.py stocksvest-app:/tmp/
    docker exec stocksvest-app python /tmp/replay_news.py /tmp/sample.json /tmp/news-replay.jsonl [limit]

sample.json: [{kind: DROP | CONTROL, symbol, date, prevDate, movePct}] (picked from a market.db copy). For each
stock-day: Alpaca news from prevDate 04:00 to date 16:00 New York, roundups (more than 5 symbols) skipped like the
watcher, the first MAX_ARTICLES read by the model with the watcher's own ask() and severity() (now = publish time).
One JSON line per article; stock-days already in the output are skipped, so a stopped run resumes. Prints the
LiteLLM key's spend before and after.
"""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from stocksvest_app.config import Settings
from stocksvest_app.market.client import MarketClient
from stocksvest_app.watchers import news

ET = ZoneInfo("America/New_York")
MAX_ARTICLES = 8  # ponytail: caps a megacap's news flood; the earliest 8 decide whether a warning came in time


def spend(s: Settings) -> float | None:
    r = requests.get(f"{s.litellm_url}/key/info", headers={"Authorization": f"Bearer {s.litellm_key}"}, timeout=30)
    return r.json().get("info", {}).get("spend") if r.ok else None


def main(sample_path, out_path, limit=None):
    s = Settings()
    market = MarketClient(s)
    out = Path(out_path)
    done = {(r["symbol"], r["date"]) for r in map(json.loads, out.read_text().splitlines())} if out.exists() else set()
    print("spend before", spend(s), flush=True)
    todo = [d for d in json.load(open(sample_path)) if (d["symbol"], d["date"]) not in done][:limit]
    lock = threading.Lock()

    def one(n, d):
        start = datetime.combine(datetime.fromisoformat(d["prevDate"]), time(4), ET)
        end = datetime.combine(datetime.fromisoformat(d["date"]), time(16), ET)
        items = market.news([d["symbol"]], start.isoformat(), end.isoformat(), 50, None, True)["news"]
        items = sorted((a for a in items if len(a["symbols"]) <= 5), key=lambda a: a["time"])[:MAX_ARTICLES]
        rows = []
        for a in items:
            ans = news.ask(s, d["symbol"], news.plain(a))
            rows.append({**d, "time": a["time"], "headline": a["headline"], "source": a.get("source"),
                         "answer": {k: ans.get(k) for k in ("kind", "eventDate", "timing", "direction",
                                                            "severity", "checked", "summary")},
                         "severity": news.severity(ans, datetime.fromisoformat(a["time"]))})
        with lock:
            for r in rows or [{**d, "time": None}]:  # a day without articles still counts as done
                f.write(json.dumps(r) + "\n")
            f.flush()
            print(n, d["kind"], d["symbol"], d["date"], len(items), max((r["severity"] for r in rows), default=0),
                  flush=True)

    with out.open("a") as f, ThreadPoolExecutor(8) as pool:  # each model call takes ~10 s
        list(pool.map(one, range(1, len(todo) + 1), todo))
    print("spend after", spend(s), flush=True)


def report(out_path, drops_path, min_severity=3):
    """Drop days: did a news event (severity >= min_severity) come before the price watcher's first alert
    (replay_watchers.py --json)? Quiet days: how often did one fire anyway (a false alarm)?

        python scripts/replay_news.py report news-replay.jsonl drops.json
    """
    rows = {(r["symbol"], r["date"], r["time"], r.get("headline")): r
            for r in map(json.loads, Path(out_path).read_text().splitlines())}.values()  # a restarted run may repeat
    alerts = {}
    for r in json.load(open(drops_path))["drops"]:
        t = datetime.combine(datetime.fromisoformat(r["date"]), time.fromisoformat(r["time"]), ET)
        alerts.setdefault(r["symbol"], []).append(t)
    days = {}
    for r in rows:
        days.setdefault((r["kind"], r["symbol"], r["date"], r["prevDate"]), []).append(r)
    res = {"DROP": {"days": 0, "noArticles": 0, "newsFirst": 0, "priceFirst": 0, "noEvent": 0, "dayBefore": 0,
                    "leadMinutes": []},
           "CONTROL": {"days": 0, "noArticles": 0, "withEvent": 0, "events": 0}}
    for (kind, sym, day, prev), arts in days.items():
        x = res[kind]
        x["days"] += 1
        arts = [a for a in arts if a["time"]]
        x["noArticles"] += not arts
        ev = sorted(datetime.fromisoformat(a["time"]).astimezone(ET) for a in arts if a["severity"] >= min_severity)
        if kind == "CONTROL":
            x["withEvent"] += bool(ev)
            x["events"] += len(ev)
            continue
        lo = datetime.combine(datetime.fromisoformat(prev), time(16), ET)  # the price alert may come after hours
        alert = min((t for t in alerts.get(sym, []) if lo <= t <= datetime.combine(datetime.fromisoformat(day),
                                                                                    time(16), ET)), default=None)
        if not ev:
            x["noEvent"] += bool(arts)
        elif alert is None or ev[0] < alert:
            x["newsFirst"] += 1
            if alert:
                x["leadMinutes"].append(round((alert - ev[0]).total_seconds() / 60))
        else:
            x["priceFirst"] += 1
        x["dayBefore"] += any(t.date().isoformat() == prev and t.hour < 16 for t in ev)
    lead = sorted(res["DROP"].pop("leadMinutes"))
    res["DROP"]["leadMinutesMedian"] = lead[len(lead) // 2] if lead else None
    res["DROP"]["leadOver60"] = sum(m > 60 for m in lead)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    if sys.argv[1] == "report":
        report(*sys.argv[2:4], *map(float, sys.argv[4:5]))
    else:
        main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
