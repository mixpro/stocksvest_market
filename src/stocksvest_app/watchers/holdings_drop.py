"""Watcher: a held stock is down `dropPercent` or more today, pre-market and after hours included (WATCH-4).

The drop is the worse of: latest price vs the previous close, and (after hours) latest price vs today's close.
It fires again at each further step (8%, 16%, 24%... with the default), once per step per day.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NAME = "holdings-drop"
EVERY = timedelta(minutes=1)
HOURS = "EXTENDED"
ENABLED = True
DEFAULTS = {"dropPercent": 8.0}
ET = ZoneInfo("America/New_York")


def _pct(a, b) -> float | None:
    return (a - b) / b * 100 if a is not None and b else None


def check(ctx, conn, now, settings, state) -> list[dict]:
    held: dict[str, list[str]] = {}
    for r in conn.execute("SELECT DISTINCT l.symbol, p.name FROM position_lots l JOIN portfolios p "
                          "ON p.id = l.portfolio_id WHERE l.closed_at IS NULL"):
        held.setdefault(r["symbol"], []).append(r["name"])
    step = float(settings["dropPercent"])
    today = now.astimezone(ET).date().isoformat()
    out = []
    for symbol, s in ctx.market.snapshots(set(held)).items():
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
