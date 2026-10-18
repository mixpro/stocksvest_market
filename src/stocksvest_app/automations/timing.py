"""When does a schedule fire? Pure functions over a schedule, a point in time and the trading calendar."""

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from itertools import count, islice
from zoneinfo import ZoneInfo

from .models import (
    WEEKDAY_INDEX,
    DailySchedule,
    EndType,
    IntervalSchedule,
    IntervalUnit,
    MarketAnchor,
    MarketSchedule,
    OnceSchedule,
    Schedule,
    WeeklySchedule,
)

# Look at most this far ahead for the next fire time.
HORIZON = timedelta(days=400)  # Used by: iter_fires
ET = ZoneInfo("America/New_York")  # MARKET schedules. Used by: _raw_fires, runner, scheduler tests

# (start, end) -> trading days, each {"date", "preMarketOpen", "regularOpen", "regularClose", "afterHoursClose"}
CalendarFn = Callable[[date, date], list[dict]]  # usually MarketData.calendar_days. Used by: every function here

# MarketAnchor -> the calendar day field holding that time. Used by: _raw_fires.
ANCHOR_FIELD = {
    MarketAnchor.PRE_MARKET_OPEN: "preMarketOpen",
    MarketAnchor.REGULAR_OPEN: "regularOpen",
    MarketAnchor.REGULAR_CLOSE: "regularClose",
    MarketAnchor.AFTER_HOURS_CLOSE: "afterHoursClose",
}


def _days_from(d: date) -> Iterator[date]:
    """Endless sequence of dates from `d` on.

    Args:
        d: first date.
    """
    for i in count():
        yield d + timedelta(days=i)


def _at(d: date, t, tz: ZoneInfo) -> datetime:
    """A local date and time as an aware datetime.

    Args:
        d: the date.
        t: a time (naive).
        tz: the schedule's zone.
    """
    return datetime.combine(d, t, tz)


def _raw_fires(s: Schedule, after: datetime, created_at: datetime, calendar: CalendarFn) -> Iterator[datetime]:
    """Candidate fire times, ascending, ignoring startAt / end / exceptions.

    Args:
        s: the schedule.
        after: start looking from this time.
        created_at: the automation's creation time (anchor for INTERVAL and WEEKLY without startAt).
        calendar: trading days for MARKET schedules.

    Used by: iter_fires.
    """
    tz = ZoneInfo(s.timezone)
    first_day = after.astimezone(tz).date()

    if isinstance(s, OnceSchedule):
        yield s.at
        return

    if isinstance(s, DailySchedule):
        times = sorted(s.atTimes)
        for d in _days_from(first_day):
            for t in times:
                yield _at(d, t, tz)

    elif isinstance(s, WeeklySchedule):
        days = {WEEKDAY_INDEX[w] for w in s.onDays}
        times = sorted(s.atTimes)
        anchor = (s.startAt or created_at).astimezone(tz).date()
        anchor_monday = anchor - timedelta(days=anchor.weekday())
        for d in _days_from(first_day):
            if d.weekday() in days and ((d - anchor_monday).days // 7) % s.everyNWeeks == 0:
                for t in times:
                    yield _at(d, t, tz)

    elif isinstance(s, IntervalSchedule):
        anchor = s.startAt or created_at
        if s.unit == IntervalUnit.DAY:
            # Whole days follow the wall clock, so a 09:30 run stays at 09:30 across DST changes.
            local = anchor.astimezone(tz)
            k = max(0, (first_day - local.date()).days // s.every - 1)
            while True:
                yield _at(local.date() + timedelta(days=k * s.every), local.timetz().replace(tzinfo=None), tz)
                k += 1
        step = timedelta(minutes=s.every) if s.unit == IntervalUnit.MINUTE else timedelta(hours=s.every)
        k = max(0, int((after - anchor) / step))
        while True:
            yield anchor + k * step
            k += 1

    elif isinstance(s, MarketSchedule):
        start = after.astimezone(ET).date()
        while True:
            chunk_end = start + timedelta(days=60)
            days = calendar(start, chunk_end)
            for day in days:
                base = datetime.fromisoformat(day[ANCHOR_FIELD[s.anchor]]) + timedelta(minutes=s.offsetMinutes)
                if not s.repeatEveryMinutes:
                    yield base
                    continue
                until = datetime.fromisoformat(day[ANCHOR_FIELD[s.repeatUntil or MarketAnchor.REGULAR_CLOSE]])
                t = base
                while t < until:  # stops before the end anchor (e.g. no run at the closing bell)
                    yield t
                    t += timedelta(minutes=s.repeatEveryMinutes)
            if not days and start > datetime.now(timezone.utc).date() + timedelta(days=30):
                return  # past the end of the published calendar
            start = chunk_end + timedelta(days=1)


def _allowed(s: Schedule, t: datetime) -> bool:
    """Whether a candidate passes the schedule's exceptions, and an INTERVAL schedule's days and window.

    Args:
        s: the schedule.
        t: candidate time.

    Used by: iter_fires.
    """
    tz = ZoneInfo(s.timezone)
    local = t.astimezone(tz)
    if local.date() in s.exceptions:
        return False
    if isinstance(s, IntervalSchedule):
        if s.onDays and local.weekday() not in {WEEKDAY_INDEX[w] for w in s.onDays}:
            return False
        if s.window and not (s.window.start <= local.time() <= s.window.end):
            return False
    return True


def iter_fires(s: Schedule, after: datetime, created_at: datetime, calendar: CalendarFn) -> Iterator[datetime]:
    """Fire times strictly after `after`, ascending, within HORIZON. Count limits (end COUNT) are applied by the
    caller, which knows how many times the schedule already fired.

    Args:
        s: the schedule.
        after: exclusive lower bound.
        created_at: the automation's creation time.
        calendar: trading days (MARKET schedules).

    Used by: next_fires, fires_between.
    """
    horizon = after + HORIZON
    for t in _raw_fires(s, after, created_at, calendar):
        if t > horizon:
            return
        if t <= after or (s.startAt and t < s.startAt):
            continue
        if s.end.type == EndType.UNTIL and t > s.end.until:
            return
        if _allowed(s, t):
            yield t


def next_fires(s: Schedule, after: datetime, created_at: datetime, calendar: CalendarFn,
               limit: int = 1, fired_count: int = 0) -> list[datetime]:
    """The next `limit` fire times after `after`, honouring an end COUNT.

    Args:
        s: the schedule.
        after: exclusive lower bound.
        created_at: the automation's creation time.
        calendar: trading days.
        limit: how many.
        fired_count: how many times it already fired (for end COUNT).

    Used by: scheduler._tick_schedule, routes (next-runs, schedule-preview, nextRunAt).
    """
    if s.end.type == EndType.COUNT:
        limit = min(limit, max(0, s.end.count - fired_count))
    return list(islice(iter_fires(s, after, created_at, calendar), limit))


def fires_between(s: Schedule, after: datetime, until: datetime, created_at: datetime, calendar: CalendarFn,
                  cap: int = 500) -> list[datetime]:
    """Fire times in (after, until], at most `cap` of them.

    Args:
        s: the schedule.
        after: exclusive lower bound.
        until: inclusive upper bound.
        created_at: the automation's creation time.
        calendar: trading days.
        cap: maximum number returned.

    Used by: scheduler._tick_schedule (times due since the last pass).
    """
    out = []
    for t in iter_fires(s, after, created_at, calendar):
        if t > until or len(out) >= cap:
            break
        out.append(t)
    return out
