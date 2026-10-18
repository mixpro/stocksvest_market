"""Background sync loop: incremental bars every few minutes during the extended session, nightly maintenance after it."""

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import db, sync
from .alpaca import AlpacaClient
from .config import Settings

log = logging.getLogger(__name__)

TICK_SECONDS = 60
# Nightly maintenance starts this long after the extended session closes (20:00 ET), once the delayed data is in.
NIGHTLY_AFTER_CLOSE = timedelta(minutes=45)


def _session(conn, day: str) -> tuple[datetime, datetime] | None:
    """The extended session of a trading day.

    Args:
        conn: market DB connection.
        day: New York date, "YYYY-MM-DD".

    Returns:
        (extended session open, extended session close), or None if the market is closed that day.

    Used by: due_jobs.
    """
    row = conn.execute("SELECT session_open, session_close FROM calendar WHERE date = ?", (day,)).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row["session_open"]), datetime.fromisoformat(row["session_close"])


def due_jobs(conn, settings: Settings, now: datetime) -> list[str]:
    """Decide which jobs should run at `now`. Pure apart from DB reads, so it is easy to test.

    Args:
        conn: market DB connection.
        settings: sip_delay_minutes and sync_interval_minutes.
        now: current time, timezone-aware.

    Returns:
        A subset of ["incremental", "nightly"]: "nightly" alone on first start (no symbols or calendar yet);
        "incremental" during the extended session (+ the data delay) when the interval has passed;
        "nightly" once per day, NIGHTLY_AFTER_CLOSE after the extended session ends (20:45 New York on
        non-trading days).

    Used by: Scheduler._loop.
    """
    if not conn.execute("SELECT 1 FROM symbols LIMIT 1").fetchone() or not conn.execute(
        "SELECT 1 FROM calendar LIMIT 1"
    ).fetchone():
        return ["nightly"]  # first start: build everything

    jobs = []
    et_now = now.astimezone(sync.ET)
    today = et_now.date().isoformat()
    session = _session(conn, today)
    delay = timedelta(minutes=settings.sip_delay_minutes)

    if session:
        open_, close = session
        # Keep syncing until the delayed data covers the whole session.
        if open_ <= now - delay and now <= close + delay + timedelta(minutes=settings.sync_interval_minutes):
            last = db.get_state(conn, "last_incremental_at")
            if not last or now - datetime.fromisoformat(last) >= timedelta(minutes=settings.sync_interval_minutes):
                jobs.append("incremental")

    nightly_time = (session[1] if session else et_now.replace(hour=20, minute=0, second=0)) + NIGHTLY_AFTER_CLOSE
    if now >= nightly_time and db.get_state(conn, "last_nightly_date") != today:
        jobs.append("nightly")
    return jobs


class Scheduler:
    """Background thread that runs due_jobs every TICK_SECONDS, one job at a time under sync.WRITE_LOCK.

    Attributes:
        running_job: "incremental" / "nightly" while one runs, else None (shown by GET /v1/status).
        last_error: last failure with its time, cleared by the next successful tick (shown by GET /v1/status).

    Used by: api.create_app (started in lifespan when RUN_SCHEDULER is on).
    """

    def __init__(self, settings: Settings):
        """Args:
            settings: service settings (DB path, Alpaca keys, intervals).
        """
        self.settings = settings
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self.running_job: str | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        """Start the background thread (daemon: it ends with the process)."""
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to stop after the current tick."""
        self._stop.set()

    def _loop(self) -> None:
        """The thread body: every tick, run the due jobs; errors are logged and kept in last_error."""
        conn = db.connect(self.settings.db_path)
        client = AlpacaClient(self.settings)
        while not self._stop.is_set():
            try:
                now = datetime.now(timezone.utc)
                for job in due_jobs(conn, self.settings, now):
                    with sync.WRITE_LOCK:
                        self.running_job = job
                        log.info("running %s sync", job)
                        if job == "nightly":
                            sync.run_nightly(conn, client, self.settings)
                        else:
                            # Leave room before the next cycle so fresh symbols never wait on the backlog.
                            budget = max(40, self.settings.sync_interval_minutes * 60 - 20)
                            sync.run_incremental(conn, client, self.settings, budget_seconds=budget)
                self.last_error = None
            except Exception as e:  # keep the loop alive; the error is shown in /v1/status
                log.exception("sync failed")
                self.last_error = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {e}"
            finally:
                self.running_job = None
            self._stop.wait(TICK_SECONDS)
