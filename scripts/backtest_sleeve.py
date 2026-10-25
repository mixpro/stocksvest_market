"""RISK-3: a buy-and-hold sleeve of the N most-traded stocks, equal weight, re-picked each quarter; alone and 50/50
with STRAT-5B. Args: scratch dir. Costs: the modelled half-spread on every share bought or sold at a rebalance."""

import json
import sys
from datetime import date, datetime, time
from pathlib import Path

from stocksvest_app.backtest.history import ET, HistoricalMarket, spread_for

B = Path(sys.argv[1])


def sleeve(h, days, n):
    """Daily value (start 1.0) of the quarterly re-picked top-n sleeve."""
    value, weights, out, q, last = 1.0, {}, {}, None, {}
    prev = None
    for d in days:
        for t in list(weights):  # mark to market (a delisted stock keeps its last close)
            c = h._daily_on(t, d)
            if c:
                last[t] = c
        if prev and weights:
            value = sum(w * last[t] / ref[t] for t, w in weights.items())
        qq = (d[:4], (int(d[5:7]) - 1) // 3)
        if qq != q:  # rebalance at the quarter's first close: pick by 20-day dollar volume before today
            h.now = datetime.combine(date.fromisoformat(d), time(9), ET)
            cands = [(h.avg_dollar_volume(t) or 0, t) for t in h.by_ticker if h._daily_on(t, d) and
                     (h._daily_on(t, d) or 0) > 10]
            pick = [t for _, t in sorted(cands, reverse=True)[:n]]
            old = {t: w * last[t] / ref[t] / value for t, w in weights.items()} if weights else {}
            new = {t: 1 / n for t in pick}
            turnover = {t: abs(new.get(t, 0) - old.get(t, 0)) for t in set(new) | set(old)}
            value *= 1 - sum(v * spread_for(h.avg_dollar_volume(t)) / 200 for t, v in turnover.items())
            weights = {t: value / n for t in pick}
            last = {t: h._daily_on(t, d) for t in pick}
            ref = dict(last)
            q = qq
        out[d] = value
        prev = d
    return out


def stats(curve, days):
    pk = dd = 0
    for d in days:
        pk = max(pk, curve[d])
        dd = min(dd, curve[d] / pk - 1)
    tot = curve[days[-1]] / curve[days[0]]
    return f"{(tot - 1) * 100:+5.0f}%  {(tot ** (1 / 8.98) - 1) * 100:4.1f}%/yr  max dd {dd * 100:4.0f}%"


def blend(a, b, days, w=0.5):
    x, y, q, prev, out = w, 1 - w, None, None, {}
    for d in days:
        if prev:
            x *= a[d] / a[prev]
            y *= b[d] / b[prev]
        qq = (d[:4], (int(d[5:7]) - 1) // 3)
        if q and qq != q:
            x, y = (x + y) * w, (x + y) * (1 - w)
        q, prev, out[d] = qq, d, x + y
    return out


for db, run in (("market10.db", "STRAT-5B"), ("market10d.db", "5B-DELISTED")):
    h = HistoricalMarket(B / db, date(2017, 11, 1), date(2017, 11, 2), hourly_lookback_days=0)
    e = {x["date"]: x["equity"] for x in json.load(open(B / f"10y-{run}.json"))["equity"]}
    days = sorted(e)
    print(f"== {db}   5B alone: {stats(e, days)}")
    for n in (30, 40, 50):
        s = sleeve(h, days, n)
        print(f"  top {n} sleeve: {stats(s, days)}   |  50/50 with 5B: {stats(blend(e, s, days), days)}")
