"""Watcher: a held stock is down dropPercent or more today, pre-market and after hours included (WATCH-4).
Prices: the live last trade (market GET /v1/quotes), else the newest minute bar from the snapshots.

The drop is the worse of: latest price vs the previous close, and (after hours) latest price vs today's close.
It fires again at each further step (8%, 16%, 24%... with the default), once per step per day.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import store

NAME = "holdings-drop"
EVERY = timedelta(minutes=1)
HOURS = "EXTENDED"
ENABLED = True
DEFAULTS = {"dropPercent": 8.0}
ET = ZoneInfo("America/New_York")


def _pct(a, b) -> float | None:
    return (a - b) / b * 100 if a is not None and b else None


def check(ctx, conn, now, settings, state) -> list[dict]:
    held = store.held(conn)
    step = float(settings["dropPercent"])
    today = now.astimezone(ET).date().isoformat()
    live = ctx.market.live_quotes(set(held))  # the last trade right now (Alpaca real-time SIP)
    out = []
    for symbol, s in ctx.market.snapshots(set(held)).items():
        q = live.get(symbol) or {}
        if q.get("last") is not None and q.get("time"):
            s = {**s, "latestPrice": q["last"], "latestTime": q["time"]}
        latest_day = (datetime.fromisoformat(s["latestTime"].replace("Z", "+00:00")).astimezone(ET).date().isoformat()
                      if s.get("latestTime") else None)
        if latest_day != today:  # no trade yet today: yesterday's move was already reported yesterday
            continue
        moves = {"since the previous close": _pct(s["latestPrice"], s.get("previousClose"))}
        if s.get("dailyDate") == today:
            moves["since today's close"] = _pct(s["latestPrice"], s.get("dailyClose"))
        label, drop = min(((k, v) for k, v in moves.items() if v is not None), key=lambda kv: kv[1], default=(None, None))
        if drop is None or step <= 0 or drop > -step:
            continue
        level = int(-drop // step)
        out.append({
            "key": f"drop:{symbol}:{today}:{level}", "kind": "HOLDING_DROP", "symbol": symbol,
            "severity": min(5, 2 + level),
            "title": f"{symbol} down {-drop:.1f}%",
            "body": f"{symbol} is at ${s['latestPrice']:,.2f}, {drop:.1f}% {label}. Held in {', '.join(held[symbol])}.",
            "data": {"price": s["latestPrice"], "changePercent": round(drop, 2), "measuredFrom": label,
                     "portfolios": held[symbol]},
        })
    return out
