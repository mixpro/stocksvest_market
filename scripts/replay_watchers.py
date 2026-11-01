"""WATCH-11 step 1: replay the price watchers over history and measure what happened after each alert.

    PYTHONPATH=src .venv/bin/python scripts/replay_watchers.py <copy of market.db> [--json out.json]

holdings-drop: the first hourly bar of a day (pre-market, regular or after hours) whose close is dropPercent (8%) or
more below the previous close (after the close also: below today's close), as in watchers/holdings_drop.py. The sale
fills at the next hourly bar's VWAP minus half the modelled spread (backtest.history.spread_for). The question: is
the stock lower 1, 3 and 10 trading days later than that fill (selling helped) or higher (selling cost)? Compared
with every stock-day's return over the same horizons (the baseline).
market: the first regular-hour bar of a day where the universe's median move is marketDropPercent (2%) or more down;
sector: a sector's median (10+ stocks) sectorDropPercent (3%) down. Forward returns of the median stock / sector.
The universe is today's halal list (stocks that left it are missing: survivorship bias, as in the backtester).
"""

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from stocksvest_app.backtest.history import spread_for

ET = ZoneInfo("America/New_York")
HORIZONS = (1, 3, 10)
DROP, MARKET_DROP, SECTOR_DROP, MIN_SECTOR = 8.0, 2.0, 3.0, 10
LIQUID = 50e6  # STRAT-5's buy gate: $50M a day


def et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ET)


def load(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    sym = {i: (t, s) for i, t, s in conn.execute("SELECT id, ticker, sector FROM symbols")}
    daily = defaultdict(list)  # ticker -> [(date, close, dollar volume)]
    for sid, ts, c, v in conn.execute("SELECT symbol_id, ts, c, v FROM bars_1day ORDER BY symbol_id, ts"):
        if sid in sym:
            daily[sym[sid][0]].append((et(ts).date().isoformat(), c, c * v))
    hourly = defaultdict(lambda: defaultdict(list))  # ticker -> date -> [(end ET, close, vwap)]
    for sid, ts, c, vw in conn.execute("SELECT symbol_id, ts, c, vw FROM bars_1hour ORDER BY symbol_id, ts"):
        if sid in sym:
            end = et(ts + 3600)
            hourly[sym[sid][0]][et(ts).date().isoformat()].append((end, c, vw or c))
    conn.close()
    return {t: s for t, s in sym.values()}, daily, hourly


def stats(xs):
    if not xs:
        return None
    return {"n": len(xs), "meanPct": round(statistics.fmean(xs), 2), "medianPct": round(statistics.median(xs), 2),
            "lowerPct": round(100 * sum(x < 0 for x in xs) / len(xs), 1)}


def session(end: datetime) -> str:
    hm = end.hour * 60 + end.minute
    return "PRE" if hm <= 9 * 60 + 30 else "REGULAR" if hm <= 16 * 60 else "AFTER"


def drops(daily, hourly):
    """One row per first alert of a stock-day, plus the baseline returns of every stock-day."""
    rows, base = [], {k: [] for k in HORIZONS}
    first_hour_day = min((d for h in hourly.values() for d in h), default="9999")
    for t, bars in daily.items():
        for i in range(21, len(bars)):
            day, close, _ = bars[i]
            if day < first_hour_day:
                continue
            adv = statistics.fmean(b[2] for b in bars[i - 20:i])
            liquid = adv >= LIQUID
            for k in HORIZONS:
                if i + k < len(bars) and liquid:
                    base[k].append(100 * (bars[i + k][1] / close - 1))
            prev = bars[i - 1][1]
            hours = hourly[t].get(day, [])
            for j, (end, c, _) in enumerate(hours):
                d = c / prev - 1
                if session(end) == "AFTER":
                    d = min(d, c / close - 1)
                if d * 100 > -DROP:
                    continue
                nxt = hours[j + 1][2] if j + 1 < len(hours) else (hourly[t].get(bars[i + 1][0], [(0, c, c)])[0][2]
                                                                  if i + 1 < len(bars) else c)
                fill = nxt * (1 - spread_for(adv) / 200)  # sell at the bid
                rows.append({"symbol": t, "date": day, "time": end.strftime("%H:%M"), "session": session(end),
                             "dropPct": round(100 * d, 1), "liquid": liquid, "fill": round(fill, 4),
                             **{f"d{k}": round(100 * (bars[i + k][1] / fill - 1), 2) for k in HORIZONS
                                if i + k < len(bars)}})
                break
    return rows, base


def market(sectors, daily, hourly):
    """First regular-hour bar of a day where the market median or a sector median is down past its threshold."""
    close = {t: {d: c for d, c, _ in b} for t, b in daily.items()}
    days = sorted({d for b in daily.values() for d, _, _ in b})
    idx = {d: i for i, d in enumerate(days)}
    moves = defaultdict(lambda: defaultdict(dict))  # date -> end -> ticker -> move %
    for t, by_day in hourly.items():
        for day, hours in by_day.items():
            i = idx.get(day)
            prev = close[t].get(days[i - 1]) if i else None
            if not prev:
                continue
            for end, c, _ in hours:
                if session(end) == "REGULAR":
                    moves[day][end][t] = 100 * (c / prev - 1)
    out = []
    for day in sorted(moves):
        fired = set()
        for end in sorted(moves[day]):
            m = moves[day][end]
            groups = {"MARKET": list(m)}
            for t in m:
                groups.setdefault(sectors.get(t) or "?", []).append(t)
            for g, ts in groups.items():
                if g in fired or (g != "MARKET" and (g == "?" or len(ts) < MIN_SECTOR)):
                    continue
                med = statistics.median(m[t] for t in ts)
                if med > -(MARKET_DROP if g == "MARKET" else SECTOR_DROP):
                    continue
                fired.add(g)
                fwd = {}
                for k in HORIZONS:
                    j = idx[day] + k
                    if j < len(days):
                        prev = days[idx[day] - 1]  # alert price = previous close x (1 + move)
                        r = [100 * (close[t][days[j]] / (close[t][prev] * (1 + m[t] / 100)) - 1)
                             for t in ts if days[j] in close[t]]
                        fwd[f"d{k}"] = round(statistics.median(r), 2) if r else None
                out.append({"group": g, "date": day, "time": end.strftime("%H:%M"), "medianPct": round(med, 2),
                            "stocks": len(ts), **fwd})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("db")
    p.add_argument("--json")
    a = p.parse_args()
    sectors, daily, hourly = load(a.db)
    rows, base = drops(daily, hourly)
    mk = market(sectors, daily, hourly)
    report = {"baselineLiquid": {f"d{k}": stats(v) for k, v in base.items()}, "drops": {}, "market": {}}
    splits = {"all": lambda r: True, "liquid": lambda r: r["liquid"], "illiquid": lambda r: not r["liquid"],
              **{f"liquid {s}": (lambda s: lambda r: r["liquid"] and r["session"] == s)(s)
                 for s in ("PRE", "REGULAR", "AFTER")},
              "liquid 8-16%": lambda r: r["liquid"] and r["dropPct"] > -16,
              "liquid 16-24%": lambda r: r["liquid"] and -24 < r["dropPct"] <= -16,
              "liquid 24%+": lambda r: r["liquid"] and r["dropPct"] <= -24}
    for name, f in splits.items():
        sel = [r for r in rows if f(r)]
        report["drops"][name] = {f"d{k}": stats([r[f"d{k}"] for r in sel if f"d{k}" in r]) for k in HORIZONS}
    for g in ("MARKET", "SECTOR"):
        sel = [r for r in mk if (r["group"] == "MARKET") == (g == "MARKET")]
        report["market"][g] = {f"d{k}": stats([r[f"d{k}"] for r in sel if r.get(f"d{k}") is not None])
                               for k in HORIZONS}
    report["marketDays"] = [r for r in mk if r["group"] == "MARKET"]
    report["examples"] = [r for r in rows if r["symbol"] in ("IOVA", "INTC") and r["date"] >= "2026-10-01"]
    print(json.dumps(report, indent=1))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"report": report, "drops": rows, "market": mk}, f)


if __name__ == "__main__":
    main()
