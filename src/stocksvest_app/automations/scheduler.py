"""Background scheduler: finds due schedule times and runs their automations, each time at most once."""

import logging
import threading
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter

from .. import db
from ..deps import AppContext
from ..runs import store as runs_store
from . import store
from .models import MissedPolicy, Schedule
from .runner import run_automation
from .timing import fires_between, next_fires

log = logging.getLogger(__name__)

# A time this late or less is "on time"; older due times are "missed" (the app was down or busy).
GRACE = timedelta(minutes=2)
MAX_CATCH_UP_RUNS = 50
_schedule = TypeAdapter(Schedule)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat() if t else None


def tick(ctx: AppContext, conn, now: datetime | None = None) -> list[dict]:
    """One scheduler pass. Returns the runs it started."""
    now = now or datetime.now(timezone.utc)
    calendar = ctx.market.calendar_days
    runs = []
    for automation in store.list_automations(conn, status="ACTIVE"):
        created = datetime.fromisoformat(automation["createdAt"])
        for raw in automation["schedules"]:
            s = _schedule.validate_python(raw)
            try:
                runs += _tick_schedule(ctx, conn, automation, s, created, calendar, now)
            except Exception:  # a broken schedule must not stop the others
                log.exception("schedule %s of automation %s failed", s.id, automation["id"])
    return runs


def _tick_schedule(ctx, conn, automation, s, created, calendar, now) -> list[dict]:
    state = store.get_state(conn, s.id)
    if state is None:
        # New, edited or resumed schedule: start from now, nothing is replayed.
        nxt = next_fires(s, now, created, calendar, fired_count=store.fire_count(conn, automation["id"], s.id))
        store.set_state(conn, automation["id"], s.id, None, _iso(nxt[0] if nxt else None))
        return []
    if not state["next_fire_at"] or datetime.fromisoformat(state["next_fire_at"]) > now:
        return []

    after = (datetime.fromisoformat(state["last_fired_for"]) if state["last_fired_for"]
             else datetime.fromisoformat(state["next_fire_at"]) - timedelta(microseconds=1))
    due = fires_between(s, after, now, created, calendar)
    remaining = None
    if s.end.type.value == "COUNT":
        remaining = max(0, s.end.count - store.fire_count(conn, automation["id"], s.id))

    if s.missedPolicy == MissedPolicy.RUN_ALL:
        to_run = due[-MAX_CATCH_UP_RUNS:]
    elif s.missedPolicy == MissedPolicy.RUN_ONCE:
        to_run = due[-1:]
    else:  # SKIP: only a time that is not late
        to_run = [t for t in due if now - t <= GRACE][-1:]
    if remaining is not None:
        to_run = to_run[:remaining]

    runs = []
    missed = [t for t in due if t not in to_run]
    if missed and s.missedPolicy == MissedPolicy.SKIP:
        # Leave a trace in the history: these times were due while the app was down or busy.
        runs.append(runs_store.add_run(
            conn, "AUTOMATION", automation["id"], automation, 0, status="SKIPPED",
            result={"trigger": {"type": "SCHEDULE", "scheduleId": s.id, "scheduledFor": _iso(missed[-1])},
                    "configVersion": automation["configVersion"], "skipReason": "MISSED",
                    "missedCount": len(missed), "missedTimes": [_iso(t) for t in missed[-20:]]},
            retention_days=ctx.settings.run_retention_days))
    for t in to_run:
        fire_id = store.claim_fire(conn, automation["id"], s.id, _iso(t))
        if fire_id:  # None = this time already fired (e.g. a second scheduler process)
            runs.append(run_automation(ctx, conn, automation, "SCHEDULE", scheduled_for=t, schedule_id=s.id,
                                       fire_id=fire_id))

    last = due[-1] if due else datetime.fromisoformat(state["next_fire_at"])
    nxt = next_fires(s, max(last, now), created, calendar,
                     fired_count=store.fire_count(conn, automation["id"], s.id))
    store.set_state(conn, automation["id"], s.id, _iso(last), _iso(nxt[0] if nxt else None))
    return runs


BROKER_SYNC_EVERY = timedelta(seconds=60)


def sync_brokers(ctx: AppContext, conn) -> None:
    """Import balances, fills and order updates for every portfolio."""
    from ..brokers import store as brokers_store
    from ..brokers.base import BrokerError
    from ..orders import service, store as orders_store
    from ..portfolios import store as portfolios_store

    for p in portfolios_store.list_portfolios(conn):
        account = brokers_store.get_account(conn, p["brokerAccountId"])
        try:
            service.sync_portfolio(conn, ctx.broker_factory(account), p)
            brokers_store.set_status(conn, account["id"], "CONNECTED", synced=True)
        except BrokerError as e:
            log.warning("broker sync for portfolio %s failed: %s", p["id"], e)
            brokers_store.set_status(conn, account["id"], "ERROR", str(e))
            orders_store.add_sync(conn, account["id"], p["id"], 0, 0, [], str(e))


class AutomationScheduler:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="automation-scheduler", daemon=True)
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None
        self._last_broker_sync: datetime | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            conn = db.connect(self.ctx.settings.db_path)
            try:
                now = datetime.now(timezone.utc)
                if not self._last_broker_sync or now - self._last_broker_sync >= BROKER_SYNC_EVERY:
                    sync_brokers(self.ctx, conn)  # before runs, so budgets and positions are current
                    self._last_broker_sync = now
                tick(self.ctx, conn)
                self.last_error = None
            except Exception as e:
                log.exception("scheduler tick failed")
                self.last_error = str(e)
            finally:
                conn.close()
                self.last_tick_at = datetime.now(timezone.utc)
            self._stop.wait(self.ctx.settings.scheduler_tick_seconds)
