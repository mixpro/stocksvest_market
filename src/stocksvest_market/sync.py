"""Price sync: backfill, incremental updates, split/dividend re-sync, pruning, calendar."""

import logging
import os
import shutil
import sqlite3
import threading
import time as monotime
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, halal, sectors, symbols
from .alpaca import AlpacaClient
from .config import Settings

log = logging.getLogger(__name__)

# The exchange time zone. Used by: sync, scheduler.due_jobs, aggregate.bucket_start, api (dates and sessions).
ET = ZoneInfo("America/New_York")

# Only one writer at a time: scheduled sync jobs and halal uploads share this lock.
WRITE_LOCK = threading.Lock()

BATCH_SIZE = 100
# A stored daily close that differs from Alpaca's by more than this means a split or dividend
# re-adjusted the history, so the symbol is re-downloaded.
ADJUSTMENT_TOLERANCE = 1e-5
# Symbols whose latest bar is newer than this are "fresh" and synced first every cycle; the rest are backlog.
# Long enough to cover weekends and holidays for daily bars.
FRESH_WINDOW = timedelta(days=5)


def _history_days(settings: Settings, timeframe: str) -> int:
    """How many days of bars are kept for a timeframe.

    Args:
        settings: provides minute/hourly/daily_history_days.
        timeframe: "1Min", "1Hour" or "1Day".

    Used by: sync_bars, prune.
    """
    return {
        "1Min": settings.minute_history_days,
        "1Hour": settings.hourly_history_days,
        "1Day": settings.daily_history_days,
    }[timeframe]


def _ts(iso: str) -> int:
    """Convert an Alpaca time to unix seconds.

    Args:
        iso: ISO time, e.g. "2026-10-19T13:30:00Z".

    Used by: _row, find_readjusted.
    """
    return int(datetime.fromisoformat(iso).timestamp())


def _row(symbol_id: str, bar: dict) -> tuple:
    """An Alpaca bar as a row for the bars_* tables.

    Args:
        symbol_id: the symbol's id (Alpaca asset id).
        bar: Alpaca bar with t, o, h, l, c, v and optionally n, vw.

    Used by: _fetch_and_store.
    """
    return (symbol_id, _ts(bar["t"]), bar["o"], bar["h"], bar["l"], bar["c"], bar["v"], bar.get("n"), bar.get("vw"))


def _store(conn: sqlite3.Connection, table: str, rows: list[tuple]) -> None:
    """Upsert bar rows (from _row) into a bars_* table; the latest, possibly partial, bar is overwritten.

    Args:
        conn: market DB connection (caller commits).
        table: "bars_1min", "bars_1hour" or "bars_1day".
        rows: tuples from _row.

    Used by: _fetch_and_store.
    """
    conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _fetch_and_store(
    conn: sqlite3.Connection,
    client: AlpacaClient,
    timeframe: str,
    tickers: dict[str, str],
    start: datetime,
    end: datetime,
    deadline: float | None = None,
) -> int:
    """Download bars for the given ticker->id map and upsert them. Returns the number of bars stored.

    Args:
        conn: market DB connection; committed every 5,000 rows so progress is kept and readers aren't blocked.
        client: Alpaca client.
        timeframe: "1Min", "1Hour" or "1Day".
        tickers: ticker -> symbol id for this batch.
        start: first bar time (inclusive).
        end: last bar time (inclusive).
        deadline: time.monotonic() value to stop at (the time-boxed backlog); None = no limit.

    Used by: sync_bars.
    Stops early at `deadline` (monotime.monotonic()); stored bars are committed, so the next run resumes after them."""
    table = db.BAR_TABLES[timeframe]
    rows, count = [], 0
    for ticker, bar in client.bars(list(tickers), timeframe, start, end):
        if ticker in tickers:
            rows.append(_row(tickers[ticker], bar))
        if len(rows) >= 5000:
            _store(conn, table, rows)
            conn.commit()  # keep progress visible and short write transactions for readers
            count += len(rows)
            rows = []
        if deadline is not None and monotime.monotonic() > deadline:
            break
    _store(conn, table, rows)
    conn.commit()
    return count + len(rows)


def sync_bars(
    conn: sqlite3.Connection,
    client: AlpacaClient,
    settings: Settings,
    timeframe: str,
    now: datetime,
    only_ids: set[str] | None = None,
    full: bool = False,
    part: str = "all",
    deadline: float | None = None,
) -> int:
    """Bring one timeframe up to date. Symbols without bars (or `full`) get the whole history window;
    the rest are fetched from their latest stored bar, which is re-fetched because it may have been partial.

    part="fresh" only syncs symbols whose latest bar is recent (cheap: a few requests for all symbols);
    part="backlog" only syncs the rest (new or partly backfilled symbols), until `deadline`.

    Args:
        conn: market DB connection.
        client: Alpaca client.
        settings: history windows and the SIP delay.
        timeframe: "1Min", "1Hour" or "1Day".
        now: the sync time; bars are requested up to now - SIP_DELAY_MINUTES.
        only_ids: limit to these symbol ids (e.g. symbols re-downloaded after a split).
        full: ignore stored bars and fetch the whole history window.
        part: "all", "fresh" or "backlog" (see above). Fresh = latest bar newer than FRESH_WINDOW.
        deadline: time.monotonic() value after which no new batch starts.

    Returns:
        Number of bars stored.

    Used by: sync_all_timeframes, run_incremental.
    """
    table = db.BAR_TABLES[timeframe]
    end = now - timedelta(minutes=settings.sip_delay_minutes)
    window_start = now - timedelta(days=_history_days(settings, timeframe))
    fresh_after = now - FRESH_WINDOW
    last = {
        r["symbol_id"]: r["last_ts"]
        for r in conn.execute(f"SELECT symbol_id, MAX(ts) AS last_ts FROM {table} GROUP BY symbol_id")
    }
    syms = [
        (r["id"], r["ticker"])
        for r in conn.execute("SELECT id, ticker FROM symbols ORDER BY ticker")
        if only_ids is None or r["id"] in only_ids
    ]

    def start_of(sid: str) -> datetime:
        """Where to start fetching for a symbol: the window start, or its latest stored bar (re-fetched).

        Args:
            sid: symbol id.
        """
        if full or sid not in last:
            return window_start
        return max(datetime.fromtimestamp(last[sid], timezone.utc), window_start)

    if part != "all":
        syms = [s for s in syms if (start_of(s[0]) >= fresh_after) == (part == "fresh")]
    # Most up-to-date symbols first, and batch symbols with similar start times together
    # so one stale symbol doesn't widen a whole batch.
    syms.sort(key=lambda s: start_of(s[0]), reverse=True)
    total = 0
    for i in range(0, len(syms), BATCH_SIZE):
        if deadline is not None and monotime.monotonic() > deadline:
            break
        batch = syms[i : i + BATCH_SIZE]
        start = min(start_of(sid) for sid, _ in batch)
        if start >= end:
            continue
        total += _fetch_and_store(conn, client, timeframe, {t: sid for sid, t in batch}, start, end, deadline)
    if syms:
        log.info("%s %s: stored %d bars for %d symbols", timeframe, part, total, len(syms))
    return total


def sync_all_timeframes(conn, client, settings, now, only_ids=None, full=False) -> dict:
    """Run sync_bars for 1Min, 1Hour and 1Day with no time limit.

    Args:
        conn: market DB connection.
        client: Alpaca client.
        settings: history windows and the SIP delay.
        now: sync time.
        only_ids: limit to these symbol ids; None = all symbols.
        full: fetch the whole history window again (after a split or dividend).

    Returns:
        {timeframe: bars stored}.

    Used by: run_nightly, api (backfill after a halal upload), __main__.main (import-halal).
    """
    return {tf: sync_bars(conn, client, settings, tf, now, only_ids, full) for tf in db.BAR_TABLES}


def find_readjusted(conn: sqlite3.Connection, client: AlpacaClient, now: datetime) -> set[str]:
    """Return symbol ids whose stored daily close no longer matches Alpaca's adjusted close,
    i.e. a split or dividend happened since the reference day and the history must be re-downloaded.

    The reference day is the 10th most recent stored trading day, compared for every symbol in batches.

    Args:
        conn: market DB connection.
        client: Alpaca client.
        now: sync time (unused beyond the call signature; the reference day comes from stored bars).

    Returns:
        Symbol ids to re-download with sync_all_timeframes(full=True).

    Used by: run_nightly.
    """
    ref = conn.execute("SELECT DISTINCT ts FROM bars_1day ORDER BY ts DESC LIMIT 1 OFFSET 9").fetchone()
    if not ref:
        return set()
    ref_ts = ref["ts"]
    stored = {
        r["symbol_id"]: r["c"] for r in conn.execute("SELECT symbol_id, c FROM bars_1day WHERE ts = ?", (ref_ts,))
    }
    ids = {r["ticker"]: r["id"] for r in conn.execute("SELECT id, ticker FROM symbols")}
    start = datetime.fromtimestamp(ref_ts, timezone.utc)
    changed = set()
    tickers = list(ids)
    for i in range(0, len(tickers), BATCH_SIZE):
        for ticker, bar in client.bars(tickers[i : i + BATCH_SIZE], "1Day", start, start + timedelta(hours=12)):
            sid = ids.get(ticker)
            if sid in stored and _ts(bar["t"]) == ref_ts:
                old = stored[sid]
                if abs(bar["c"] - old) > ADJUSTMENT_TOLERANCE * max(abs(old), 1e-9):
                    changed.add(sid)
    return changed


def prune(conn: sqlite3.Connection, settings: Settings, now: datetime) -> None:
    """Delete bars older than each timeframe's history window.

    Args:
        conn: market DB connection.
        settings: daily/hourly/minute_history_days.
        now: reference time.

    Used by: run_nightly.
    """
    with conn:
        for tf, table in db.BAR_TABLES.items():
            cutoff = int((now - timedelta(days=_history_days(settings, tf))).timestamp())
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))


def _et_iso(day: str, hhmm: str) -> str:
    """Combine a New York date and time into an ISO datetime with offset.

    Args:
        day: "YYYY-MM-DD".
        hhmm: "HH:MM" or "HHMM" as Alpaca sends it.

    Returns:
        E.g. "2026-10-19T09:30:00-04:00".

    Used by: sync_calendar.
    """
    hhmm = hhmm.replace(":", "")
    t = time(int(hhmm[:2]), int(hhmm[2:4]))
    return datetime.combine(date.fromisoformat(day), t, ET).isoformat()


def sync_calendar(conn: sqlite3.Connection, client: AlpacaClient, settings: Settings, today: date) -> int:
    """Replace the trading calendar from (history window + 10 days) ago to 90 days ahead.

    Args:
        conn: market DB connection.
        client: Alpaca client.
        settings: daily_history_days.
        today: New York date.

    Returns:
        Number of trading days stored.

    Used by: run_nightly, __main__.main (import-halal on an empty DB).
    """
    start = today - timedelta(days=settings.daily_history_days + 10)
    days = client.calendar(start, today + timedelta(days=90))
    rows = [
        (
            d["date"],
            _et_iso(d["date"], d.get("session_open", "0400")),
            _et_iso(d["date"], d["open"]),
            _et_iso(d["date"], d["close"]),
            _et_iso(d["date"], d.get("session_close", "2000")),
        )
        for d in days
    ]
    with conn:
        conn.execute("DELETE FROM calendar WHERE date >= ?", (start.isoformat(),))
        conn.executemany("INSERT OR REPLACE INTO calendar VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def sync_symbols(
    conn: sqlite3.Connection,
    client: AlpacaClient,
    settings: Settings,
    compliant: set[str] | None = None,
    force: bool = False,
) -> dict:
    """Rebuild the symbol universe: NASDAQ/NYSE listings, Alpaca tradable assets, and halal compliance.

    Args:
        conn: market DB connection.
        client: Alpaca client (assets).
        settings: halal_db_path and other_exchanges.
        compliant: compliant tickers to use; None = read the current halal.db.
        force: allow removing more than half the symbols (see symbols.reconcile).

    Returns:
        Summary from symbols.reconcile (universe, added, removed, compliant_in_halal_db).

    Raises:
        SymbolSyncError: no halal.db yet, a bad download, or too many removals.
        HalalDbError: halal.db is unreadable.

    Used by: run_nightly, import_halal.
    """
    if compliant is None:
        if not settings.halal_db_path.exists():
            raise symbols.SymbolSyncError(f"no halal DB at {settings.halal_db_path}; upload one first")
        compliant = halal.compliant_tickers(settings.halal_db_path)
    listings = symbols.fetch_listings(["NASDAQ"] + settings.other_exchanges)
    return symbols.reconcile(conn, listings, client.assets(), compliant, force=force)


def import_halal(conn: sqlite3.Connection, client: AlpacaClient, settings: Settings, path, force: bool = False) -> dict:
    """Validate a new halal DB, apply it to the symbols table, then make it the current halal DB.
    Nothing changes if validation or the symbol sync fails. Price backfill for added symbols is left to the caller.

    Args:
        conn: market DB connection.
        client: Alpaca client.
        settings: where halal.db lives.
        path: the uploaded/new halal DB file.
        force: allow removing more than half the symbols.

    Returns:
        Summary from sync_symbols. A halal_imports row is recorded.

    Raises:
        HalalDbError: invalid file or no compliant symbols.
        SymbolSyncError: see sync_symbols.

    Used by: api.upload_halal (POST /admin/halal), __main__.main (import-halal).
    """
    compliant = halal.compliant_tickers(path)
    if not compliant:
        raise halal.HalalDbError("halal DB has no COMPLIANT symbols")
    summary = sync_symbols(conn, client, settings, compliant=compliant, force=force)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    staged = settings.halal_db_path.with_suffix(".staged")
    shutil.copyfile(path, staged)
    os.replace(staged, settings.halal_db_path)
    with conn:
        conn.execute(
            "INSERT INTO halal_imports(imported_at, compliant_count, added, removed) VALUES (?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                len(compliant),
                len(summary["added"]),
                len(summary["removed"]),
            ),
        )
    return summary


def run_incremental(conn, client, settings, now=None, budget_seconds: float | None = None) -> dict:
    """Update every up-to-date symbol first, then spend what is left of `budget_seconds` on the backlog
    (new or partly backfilled symbols). Without a budget the backlog is finished completely.

    Args:
        conn: market DB connection.
        client: Alpaca client.
        settings: history windows and the SIP delay.
        now: sync time (default: now).
        budget_seconds: time box for the backlog; None = finish it.

    Returns:
        {"<timeframe> fresh"|"<timeframe> backlog": bars stored}. Sets last_incremental_at.

    Used by: scheduler.Scheduler._loop (every few minutes in the session), api (POST /admin/sync), __main__.main.
    """
    now = now or datetime.now(timezone.utc)
    result = {f"{tf} fresh": sync_bars(conn, client, settings, tf, now, part="fresh") for tf in db.BAR_TABLES}
    deadline = monotime.monotonic() + budget_seconds if budget_seconds is not None else None
    # Daily backfill is a few requests per symbol, hourly is ~50, so do daily first.
    for tf in ("1Day", "1Min", "1Hour"):
        result[f"{tf} backlog"] = sync_bars(conn, client, settings, tf, now, part="backlog", deadline=deadline)
    db.set_state(conn, "last_incremental_at", now.isoformat(timespec="seconds"))
    return result


def run_nightly(conn, client, settings, now=None) -> dict:
    """Refresh calendar and symbols, fill gaps (including new symbols), re-sync re-adjusted symbols, prune.
    Sectors are looked up too when SEC_USER_AGENT is set (failures there never stop the price sync).

    Args:
        conn: market DB connection.
        client: Alpaca client.
        settings: service settings (halal.db, SEC contact, history windows).
        now: sync time (default: now).

    Returns:
        {"calendar_days", "symbols", "sectors"?, "bars", "readjusted"}. Sets last_nightly_at / last_nightly_date.

    Used by: scheduler.Scheduler._loop (after the close, and on first start), api (POST /admin/sync), __main__.main.
    """
    now = now or datetime.now(timezone.utc)
    result = {"calendar_days": sync_calendar(conn, client, settings, now.astimezone(ET).date())}
    result["symbols"] = sync_symbols(conn, client, settings)
    sec = sectors.make_client(settings)
    if sec:
        try:
            result["sectors"] = sectors.sync_sectors(conn, sec)
        except Exception as e:  # sectors are optional; never block the price sync
            log.warning("sector sync failed: %s", e)
            result["sectors"] = {"error": str(e)}
    result["bars"] = sync_all_timeframes(conn, client, settings, now)
    readjusted = find_readjusted(conn, client, now)
    if readjusted:
        log.info("re-downloading %d symbols after splits/dividends", len(readjusted))
        sync_all_timeframes(conn, client, settings, now, only_ids=readjusted, full=True)
    result["readjusted"] = len(readjusted)
    prune(conn, settings, now)
    db.set_state(conn, "last_nightly_at", now.isoformat(timespec="seconds"))
    db.set_state(conn, "last_nightly_date", now.astimezone(ET).date().isoformat())
    return result
