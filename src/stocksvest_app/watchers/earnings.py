"""Watcher: a held stock reports results today or tomorrow, with its run-up over the past month (WATCH-9).

The date: the company's own announcement, kept by the news watcher (state "dates", from checked EARNINGS_DATE
answers), else the estimate from SEC 8-K item 2.02 filings (last + 91 days; within 3 days of the real date only 46%
of the time, so an estimate fires 3 days either side of it, one severity lower).

Not a sell signal. Since 2017, liquid stocks ($50M a day, 9,465 reports) fell 10% or more on 7.5% of reports; after
a 20%+ month before the report, on 13-16%. But the average reaction stayed positive (+2% for 20-30% run-ups), so
selling before reports costs on average (also FEAT-6's backtest). A run-up of runUpPercent or more raises the
severity: the odds of a big drop about double.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import news, store

NAME = "earnings"
EVERY = timedelta(minutes=30)
HOURS = "ALWAYS"
ENABLED = True
DEFAULTS = {"runUpPercent": 20.0}
EVENT_KINDS = ("EARNINGS_SOON",)
ET = ZoneInfo("America/New_York")
ESTIMATE_DAYS = 3  # an estimated date fires this many days either side
ODDS = ("Since 2017, liquid stocks up 20%+ in the month before results fell 10%+ on 13-16% of reports (7.5% overall), "
        "yet gained on average.")


def check(ctx, conn, now, settings, state) -> list[dict]:
    held = store.held(conn)
    symbols = set(held) & ctx.market.universe().keys()
    if not symbols:
        return []
    today = now.astimezone(ET).date()
    announced = store.config(conn, news)["state"].get("dates", {})
    indicators = ctx.market.indicators(symbols)
    month = ctx.market.price_changes(symbols, 1, "MONTHS")
    out = []
    for sym in sorted(symbols):
        a = announced.get(sym)
        if a and 0 <= (date.fromisoformat(a["date"]) - today).days <= 1:
            day, timing, estimated = date.fromisoformat(a["date"]), a.get("timing", "UNKNOWN"), False
            if day == today and timing == "BEFORE_OPEN" and now.astimezone(ET).hour >= 9:
                continue  # already out: the news watcher reads the results
        else:
            dte = (indicators.get(sym) or {}).get("daysToEarnings")
            if dte is None or abs(dte) > ESTIMATE_DAYS:
                continue
            day, timing, estimated = today + timedelta(days=dte), "UNKNOWN", True
            if a and abs((date.fromisoformat(a["date"]) - day).days) <= 14:
                continue  # the announced date of this report is known: it fires (or fired) on that one
        run_up = (month.get(sym) or {}).get("changePercent")
        hot = run_up is not None and run_up >= settings["runUpPercent"]
        when = "today" if day == today else "tomorrow" if day == today + timedelta(days=1) else f"around {day:%b %-d}"
        timing_text = {"BEFORE_OPEN": " before the open", "AFTER_CLOSE": " after the close"}.get(timing, "")
        out.append({
            "key": f"earnings:{sym}:{day}", "kind": "EARNINGS_SOON", "symbol": sym,
            "severity": 3 + hot - estimated,
            "title": f"{sym} reports {'(estimated) ' if estimated else ''}{when}{timing_text}",
            "body": (f"{'Estimated from its last report (+91 days). ' if estimated else ''}"
                     f"{sym} is {'up' if (run_up or 0) >= 0 else 'down'} {abs(run_up or 0):.0f}% in the past month. "
                     f"{ODDS + ' ' if hot else ''}Held in {', '.join(held[sym])}."),
            "data": {"date": day.isoformat(), "timing": timing, "estimated": estimated, "runUpPercent": run_up,
                     "articleId": a.get("articleId") if a and not estimated else None},
        })
    return out
