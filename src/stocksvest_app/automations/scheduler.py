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
GRACE = timedelta(minutes=2)  # Used by: _tick_schedule (late vs on time)
MAX_CATCH_UP_RUNS = 50  # RUN_ALL cap. Used by: _tick_schedule
_schedule = TypeAdapter(Schedule)


def _iso(t: datetime | None) -> str | None:
    """A datetime as ISO UTC, or None.

    Args:
        t: the time.
    """
    return t.astimezone(timezone.utc).isoformat() if t else None


def tick(ctx: AppContext, conn, now: datetime | None = None) -> list[dict]:
    """One scheduler pass. Returns the runs it started.

    Args:
        ctx: app context.
        conn: app DB connection.
        now: the pass time (tests pin it); default now.

    Used by: AutomationScheduler._loop, tests.
    """
    now = now or datetime.now(timezone.utc)
    calendar = ctx.market.calendar_days
    runs = []
    for automation in store.list_automations(conn, status="ACTIVE"):
        created = datetime.fromisoformat(automation["createdAt"])
        for raw in automation["schedules"]:
            s = _schedule.validate_python(raw)
            try:
                runs += _tick_schedule(ctx, conn, automation, s, created, calendar, now)
            except Exception as e:  # a broken schedule must not stop the others
                log.exception("schedule %s of automation %s failed", s.id, automation["id"])
                from ..audit import store as audit
                audit.record(conn, "SCHEDULE_ERROR", "AUTOMATION", automation["id"], source="SCHEDULER",
                             related={"scheduleId": s.id}, outcome="FAILED", error=str(e))
    return runs


def _tick_schedule(ctx, conn, automation, s, created, calendar, now) -> list[dict]:
    """Handle one schedule of an ACTIVE automation: plan it the first time, then run what is due (by its missed
    policy and end COUNT), each time claimed once, and record skipped/missed times.

    Args:
        ctx: app context.
        conn: app DB connection.
        automation: the automation dict.
        s: the validated schedule.
        created: the automation's creation time.
        calendar: trading days (MarketData.calendar_days).
        now: the pass time.

    Returns:
        Runs started (and SKIPPED/MISSED records).
    """
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

    from ..audit import store as audit

    runs = []
    missed = [t for t in due if t not in to_run]
    if missed:
        audit.record(conn, "AUTOMATION_RUNS_MISSED", "AUTOMATION", automation["id"], source="SCHEDULER",
                     related={"scheduleId": s.id}, details={"count": len(missed), "policy": s.missedPolicy.value,
                                                            "times": [_iso(t) for t in missed[-20:]]})
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
        if not fire_id:  # this time already fired (e.g. a second scheduler process)
            continue
        tokens = (audit.correlation_id.set(fire_id), audit.current_source.set("SCHEDULER"),
                  audit.current_actor.set("scheduler"))
        try:
            run = run_automation(ctx, conn, automation, "SCHEDULE", scheduled_for=t, schedule_id=s.id, fire_id=fire_id)
            audit.record(conn, "AUTOMATION_RUN", "AUTOMATION", automation["id"],
                         related={"runId": run["id"], "scheduleId": s.id},
                         details={"scheduledFor": _iso(t), "status": run["status"],
                                  "skipReason": (run.get("result") or {}).get("skipReason"),
                                  "metrics": (run.get("result") or {}).get("metrics")},
                         outcome="FAILED" if run["status"] == "FAILED" else "OK", error=run.get("error"))
            runs.append(run)
        finally:
            for var, token in zip((audit.correlation_id, audit.current_source, audit.current_actor), tokens):
                var.reset(token)

    last = due[-1] if due else datetime.fromisoformat(state["next_fire_at"])
    nxt = next_fires(s, max(last, now), created, calendar,
                     fired_count=store.fire_count(conn, automation["id"], s.id))
    store.set_state(conn, automation["id"], s.id, _iso(last), _iso(nxt[0] if nxt else None))
    return runs


BROKER_SYNC_EVERY = timedelta(seconds=60)  # Used by: AutomationScheduler._loop


def sync_brokers(ctx: AppContext, conn) -> None:
    """Import balances, fills and order updates for every broker account that has a portfolio.

    Args:
        ctx: app context (broker factory).
        conn: app DB connection.

    Used by: AutomationScheduler._loop (every BROKER_SYNC_EVERY).
    """
    from ..brokers import store as brokers_store
    from ..brokers.base import BrokerError
    from ..orders import service

    for account in brokers_store.list_accounts(conn):
        if not conn.execute("SELECT 1 FROM portfolios WHERE broker_account_id = ?", (account["id"],)).fetchone():
            continue
        try:
            service.run_account_sync(conn, ctx.broker_factory, account)
        except BrokerError as e:
            log.warning("broker sync for account %s failed: %s", account["id"], e)


def watch_held_symbols(ctx: AppContext, conn) -> list[str]:
    """Ask the market service for real-time IBKR bars of every symbol a portfolio holds or has an order open for,
    so sell filters on them (price change, unrealized gain/loss) see current prices. The watch lasts 10 minutes and
    is renewed every minute; failures are only logged.

    Args:
        ctx: app context (market API).
        conn: app DB connection.

    Returns:
        The symbols sent.

    Used by: AutomationScheduler._loop (with each broker sync).
    """
    from ..market.client import MarketError

    symbols = sorted(r["symbol"] for r in conn.execute(
        "SELECT symbol FROM position_lots WHERE closed_at IS NULL UNION "
        "SELECT symbol FROM orders WHERE status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')"))
    try:
        ctx.market_api.live_watch(symbols, 10)
    except MarketError as e:
        log.warning("asking for live bars failed: %s", e.detail)
    return symbols


class AutomationScheduler:
    """Background thread: every scheduler_tick_seconds, sync brokers (each minute), run due schedules and take
    history snapshots. Errors are logged, kept in last_error and recorded in the activity log.

    Attributes:
        last_tick_at: time of the last pass (shown by GET /v1/automations/switch).
        last_error: last failure, cleared by the next good pass.

    Used by: api.create_app (when APP_RUN_SCHEDULER is on), automations.routes.get_switch.
    """
    def __init__(self, ctx: AppContext):
        """Args:
            ctx: app context.
        """
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="automation-scheduler", daemon=True)
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None
        self._last_broker_sync: datetime | None = None

    def start(self) -> None:
        """Start the background thread (daemon)."""
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to stop after the current pass."""
        self._stop.set()

    def _loop(self) -> None:
        """The thread body: one pass per tick on a fresh connection."""
        while not self._stop.is_set():
            conn = db.connect(self.ctx.settings.db_path)
            try:
                now = datetime.now(timezone.utc)
                if not self._last_broker_sync or now - self._last_broker_sync >= BROKER_SYNC_EVERY:
                    sync_brokers(self.ctx, conn)  # before runs, so budgets and positions are current
                    watch_held_symbols(self.ctx, conn)  # real-time bars for what the portfolios hold
                    from ..orders import halal_exit
                    halal_exit.run(self.ctx, conn, now)  # sell what is held of stocks no longer halal
                    self._last_broker_sync = now
                tick(self.ctx, conn)
                from ..performance import snapshots
                snapshots.scheduled(self.ctx, conn, now)
                from . import kill_rule
                kill_rule.check(self.ctx, conn)  # after the close snapshot: pause buys of a portfolio past its limit
                self.last_error = None
                try:  # alerts never get in the way of trading
                    from ..alerts import checks
                    checks.check(self.ctx, conn, now)
                except Exception:
                    log.exception("alert checks failed")
            except Exception as e:
                log.exception("scheduler tick failed")
                self.last_error = str(e)
                from ..audit import store as audit
                audit.record(conn, "SCHEDULER_ERROR", source="SCHEDULER", actor="scheduler", outcome="FAILED",
                             error=str(e))
            finally:
                conn.close()
                self.last_tick_at = datetime.now(timezone.utc)
            self._stop.wait(self.ctx.settings.scheduler_tick_seconds)
