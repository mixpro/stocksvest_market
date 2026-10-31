"""The watcher thread: runs each enabled watcher when it is due and inside its hours, stores the events it returns
(each key once), then dispatches the new ones (dispatch.py). Apart from the automation scheduler, so a slow watcher
never delays trading. Design: UML/WatchersFlow.mmd, UML/WatcherTick.mmd.

A watcher is a module with NAME, EVERY (timedelta), HOURS (REGULAR | EXTENDED | ALWAYS), ENABLED (default on/off),
DEFAULTS (its settings) and check(ctx, conn, now, settings, state) -> list of events (see store.add). It may change
`state` in place; it is saved after a good pass. Adding one = one file + one line in WATCHERS.
"""

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..audit import store as audit
from ..market.client import MarketError
from . import dispatch, holdings_drop, market, news, store

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
TICK_SECONDS = 15  # Used by: WatcherThread._loop
WATCHERS = {w.NAME: w for w in (holdings_drop, market, news)}


def in_hours(day: dict | None, hours: str, now: datetime) -> bool:
    """Whether `now` is inside a watcher's hours on this trading day (None = market closed today).

    Used by: tick.
    """
    if hours == "ALWAYS":
        return True
    if not day:
        return False
    start, end = ("regularOpen", "regularClose") if hours == "REGULAR" else ("preMarketOpen", "afterHoursClose")
    return datetime.fromisoformat(day[start]) <= now < datetime.fromisoformat(day[end])


def tick(ctx, conn, now: datetime | None = None) -> list[str]:
    """One pass over the watchers, then dispatch. A failing watcher records its error and the others still run.

    Args:
        ctx: app context.
        conn: app DB connection.
        now: the pass time (tests pin it).

    Returns:
        Ids of the events stored in this pass.

    Used by: WatcherThread._loop, tests.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date().isoformat()
    day: dict | None | bool = False  # False = not looked up yet
    found = []
    for w in WATCHERS.values():
        c = store.config(conn, w)
        if not c["enabled"] or (c["lastRunAt"] and now - datetime.fromisoformat(c["lastRunAt"]) < w.EVERY):
            continue
        if w.HOURS != "ALWAYS" and day is False:
            try:
                days = ctx.market.calendar_days(today, today)
                day = days[0] if days else None
            except MarketError as e:
                log.warning("watchers: no calendar: %s", e.detail)
                day = None
        if not in_hours(day or None, w.HOURS, now):
            continue
        try:
            events = w.check(ctx, conn, now, c["settings"], c["state"])
        except Exception as e:  # one broken watcher must not stop the others
            log.exception("watcher %s failed", w.NAME)
            if c["lastError"] != str(e):  # once per new error, not every pass
                audit.record(conn, "WATCHER_ERROR", "WATCHER", w.NAME, source="SCHEDULER", outcome="FAILED",
                             error=str(e))
            store.save_config(conn, w, lastRunAt=db.iso(now), lastError=str(e))
            continue
        store.save_config(conn, w, state=c["state"], lastRunAt=db.iso(now), lastError=None)
        found += [eid for ev in events if (eid := store.add(conn, w.NAME, ev))]
    dispatch.dispatch(ctx, conn, now)
    return found


class WatcherThread:
    """Background thread: a watcher pass every TICK_SECONDS on a fresh connection. Errors are logged.

    Used by: api.create_app (when APP_RUN_SCHEDULER is on).
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="watchers", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            conn = db.connect(self.ctx.settings.db_path)
            try:
                tick(self.ctx, conn)
            except Exception:
                log.exception("watcher pass failed")
            finally:
                conn.close()
            self._stop.wait(TICK_SECONDS)
