"""Watcher: the whole halal universe, or one sector, falling today (WATCH-5). Regular session only: in pre-market
and after hours too few stocks trade for the median to mean much.

Fires when the median daily change of the stocks that traded today is at or below -marketDropPercent (all stocks)
or -sectorDropPercent (a sector with at least minSectorStocks), again at each further step, once per step per day.
"""

from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

NAME = "market"
EVERY = timedelta(minutes=5)
HOURS = "REGULAR"
ENABLED = True
DEFAULTS = {"marketDropPercent": 2.0, "sectorDropPercent": 3.0, "minSectorStocks": 10}
EVENT_KINDS = ("MARKET_DROP", "SECTOR_DROP")
ET = ZoneInfo("America/New_York")


def _event(key, kind, name, changes, step, today, sector=None) -> dict | None:
    m = median(changes)
    if step <= 0 or m > -step:
        return None
    level = int(-m // step)
    down = sum(c < 0 for c in changes) / len(changes) * 100
    return {"key": f"{key}:{today}:{level}", "kind": kind, "sector": sector, "severity": min(5, 2 + level),
            "title": f"{name} down {-m:.1f}% today",
            "body": f"Median stock {m:.1f}%, {down:.0f}% of {len(changes)} stocks are down.",
            "data": {"medianChangePercent": round(m, 2), "downPercent": round(down, 1), "stocks": len(changes)}}


def check(ctx, conn, now, settings, state) -> list[dict]:
    today = now.astimezone(ET).date().isoformat()
    universe = ctx.market.universe()
    by_sector: dict[str, list[float]] = {}
    changes = []
    for t, s in ctx.market_api.snapshots()["snapshots"].items():
        if (s.get("dailyChangePercent") is None or not s.get("latestTime")
                or datetime.fromisoformat(s["latestTime"].replace("Z", "+00:00")).astimezone(ET).date().isoformat() != today):
            continue
        changes.append(s["dailyChangePercent"])
        if sector := (universe.get(t) or {}).get("sector"):
            by_sector.setdefault(sector, []).append(s["dailyChangePercent"])
    if not changes:
        return []
    names = {o["value"]: o["label"] for o in ctx.market.sector_options()}
    out = [_event("market", "MARKET_DROP", "Halal stocks", changes, float(settings["marketDropPercent"]), today)]
    out += [_event(f"sector:{k}", "SECTOR_DROP", names.get(k, k), v, float(settings["sectorDropPercent"]), today, k)
            for k, v in by_sector.items() if len(v) >= int(settings["minSectorStocks"])]
    return [e for e in out if e]
