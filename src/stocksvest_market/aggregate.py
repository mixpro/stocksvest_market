"""Roll daily bars up into weekly, monthly and yearly bars."""

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from .sync import ET


def bucket_start(ts: int, timeframe: str) -> date:
    # Daily bars start at midnight ET, so the ET date is the trading day.
    d = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET).date()
    if timeframe == "1Week":
        return d - timedelta(days=d.weekday())  # Monday
    if timeframe == "1Month":
        return d.replace(day=1)
    if timeframe == "1Year":
        return d.replace(month=1, day=1)
    raise ValueError(f"unsupported timeframe {timeframe}")


def aggregate(daily: Iterable[dict], timeframe: str) -> list[dict]:
    """`daily` must be sorted by ts. Open = first open, close = last close, high/low = extremes,
    volume and trade count summed, vwap volume-weighted. `ts` is the first trading day in the bucket."""
    out: list[dict] = []
    key = None
    for bar in daily:
        k = bucket_start(bar["ts"], timeframe)
        if k != key:
            key = k
            out.append({**bar, "_pv": (bar["vw"] or 0) * bar["v"]})
            continue
        cur = out[-1]
        cur["h"] = max(cur["h"], bar["h"])
        cur["l"] = min(cur["l"], bar["l"])
        cur["c"] = bar["c"]
        cur["v"] += bar["v"]
        cur["n"] = (cur["n"] or 0) + (bar["n"] or 0)
        cur["_pv"] += (bar["vw"] or 0) * bar["v"]
    for cur in out:
        pv = cur.pop("_pv")
        cur["vw"] = pv / cur["v"] if cur["v"] else None
    return out
