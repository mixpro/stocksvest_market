"""HTTP API. Read endpoints (/v1) need an API key; /admin endpoints need the admin key."""

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile

from . import aggregate, db, halal, sectors, sync
from .alpaca import AlpacaClient
from .config import Settings
from .scheduler import Scheduler
from .symbols import SymbolSyncError

log = logging.getLogger(__name__)

MAX_LIMIT = 50000


class Timeframe(str, Enum):
    MIN = "1Min"
    HOUR = "1Hour"
    DAY = "1Day"
    WEEK = "1Week"
    MONTH = "1Month"
    YEAR = "1Year"


class Session(str, Enum):
    ALL = "all"  # pre-market + regular + after-hours
    REGULAR = "regular"


class PeriodUnit(str, Enum):
    MINUTES = "MINUTES"
    HOURS = "HOURS"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    MONTHS = "MONTHS"
    YEARS = "YEARS"


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str | None, default: datetime) -> int:
    if not value:
        return int(default.timestamp())
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"invalid time '{value}', use ISO 8601 (2026-10-01 or 2026-10-01T13:30:00Z)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=sync.ET) if "T" in value else datetime.combine(dt.date(), datetime.min.time(), sync.ET)
    return int(dt.timestamp())


def _bar_json(r: dict) -> dict:
    return {"t": _iso(r["ts"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"], "n": r["n"], "vw": r["vw"]}


def _symbol_json(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "ticker": r["ticker"],
        "name": r["name"],
        "exchange": r["exchange"],
        "assetType": r["asset_type"],
        "sector": r["sector"],  # key from /v1/sectors; null until SEC data is synced
        "sic": r["sic"],
        "sicDescription": r["sic_description"],
        "halalCompliant": True,  # only compliant symbols are stored
    }


def _period(amount: int, unit: PeriodUnit) -> timedelta:
    days = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30.4375, "YEARS": 365.25}
    if unit == PeriodUnit.MINUTES:
        return timedelta(minutes=amount)
    if unit == PeriodUnit.HOURS:
        return timedelta(hours=amount)
    return timedelta(days=amount * days[unit.value])


def create_app(settings: Settings | None = None, client_factory: Callable[[Settings], AlpacaClient] = AlpacaClient):
    settings = settings or Settings()
    state: dict = {"scheduler": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        db.init(conn)
        conn.close()
        if settings.run_scheduler:
            state["scheduler"] = Scheduler(settings)
            state["scheduler"].start()
        yield
        if state["scheduler"]:
            state["scheduler"].stop()

    app = FastAPI(
        title="StocksVest Market",
        description="Halal-compliant NYSE/NASDAQ symbols and price history, synced from Alpaca.",
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if not x_api_key or x_api_key not in settings.api_keys + ([settings.admin_key] if settings.admin_key else []):
            raise HTTPException(401, "missing or invalid X-API-Key")

    def require_admin(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if not settings.admin_key or x_api_key != settings.admin_key:
            raise HTTPException(401, "missing or invalid admin X-API-Key")

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    read = [Depends(require_key)]
    admin = [Depends(require_admin)]

    def find_symbol(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM symbols WHERE ticker = ? OR id = ?", (symbol.upper(), symbol)).fetchone()
        if not row:
            raise HTTPException(404, f"unknown symbol '{symbol}' (not listed, or not halal compliant)")
        return row

    def symbol_rows(conn: sqlite3.Connection, symbols: str | None) -> list[sqlite3.Row]:
        if not symbols:
            return conn.execute("SELECT * FROM symbols ORDER BY ticker").fetchall()
        return [find_symbol(conn, s.strip()) for s in symbols.split(",") if s.strip()]

    def load_bars(
        conn, symbol_id: str, timeframe: Timeframe, start: str | None, end: str | None, limit: int, session: Session
    ) -> list[dict]:
        stored = timeframe.value if timeframe.value in db.BAR_TABLES else "1Day"
        now = datetime.now(timezone.utc)
        start_ts = _parse_time(start, now - timedelta(days=3650))
        end_ts = _parse_time(end, now)
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT ts, o, h, l, c, v, n, vw FROM {db.BAR_TABLES[stored]} "
                "WHERE symbol_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (symbol_id, start_ts, end_ts),
            )
        ]
        if session == Session.REGULAR and stored in ("1Min", "1Hour"):
            hours = {
                r["date"]: (_parse_time(r["open"], now), _parse_time(r["close"], now))
                for r in conn.execute("SELECT date, open, close FROM calendar")
            }

            def regular(ts: int) -> bool:
                day = datetime.fromtimestamp(ts, timezone.utc).astimezone(sync.ET).date().isoformat()
                return day in hours and hours[day][0] <= ts < hours[day][1]

            rows = [r for r in rows if regular(r["ts"])]
        if stored != timeframe.value:
            rows = aggregate.aggregate(rows, timeframe.value)
        return rows[-limit:]

    # ---------------------------------------------------------------- public

    @app.get("/health", tags=["status"])
    def health():
        return {"ok": True}

    @app.get("/v1/status", tags=["status"], dependencies=read)
    def status(conn: Conn):
        counts = {tf: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for tf, t in db.BAR_TABLES.items()}
        last_import = conn.execute("SELECT * FROM halal_imports ORDER BY id DESC LIMIT 1").fetchone()
        sched = state["scheduler"]
        return {
            "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "bars": counts,
            "lastIncrementalAt": db.get_state(conn, "last_incremental_at"),
            "lastNightlyAt": db.get_state(conn, "last_nightly_at"),
            "lastHalalImport": dict(last_import) if last_import else None,
            "runningJob": sched.running_job if sched else None,
            "lastError": sched.last_error if sched else None,
            "dataDelayMinutes": settings.sip_delay_minutes,
            "sectorsEnabled": bool(settings.sec_user_agent),
        }

    @app.get("/v1/symbols", tags=["symbols"], dependencies=read)
    def list_symbols(
        conn: Conn,
        search: str | None = None,
        exchange: str | None = None,
        asset_type: Annotated[str | None, Query(alias="assetType")] = None,
        sector: Annotated[str | None, Query(description="Comma-separated sector keys")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        """The halal universe. Use as the UNIVERSE input of a filter."""
        where, args = [], []
        if search:
            where.append("(ticker LIKE ? OR name LIKE ?)")
            args += [f"{search.upper()}%", f"%{search}%"]
        if exchange:
            where.append("exchange = ?")
            args.append(exchange)
        if asset_type:
            where.append("asset_type = ?")
            args.append(asset_type)
        if sector:
            keys = [k.strip() for k in sector.split(",") if k.strip()]
            where.append(f"sector IN ({','.join('?' * len(keys))})")
            args += keys
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM symbols {clause}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM symbols {clause} ORDER BY ticker LIMIT ? OFFSET ?", args + [limit, offset])
        return {"total": total, "symbols": [_symbol_json(r) for r in rows]}

    @app.get("/v1/sectors", tags=["symbols"], dependencies=read)
    def list_sectors(conn: Conn):
        """Sector keys and names with the number of halal symbols in each (SIC-based, approximate GICS)."""
        counts = dict(conn.execute("SELECT sector, COUNT(*) FROM symbols WHERE sector IS NOT NULL GROUP BY sector"))
        unassigned = conn.execute("SELECT COUNT(*) FROM symbols WHERE sector IS NULL").fetchone()[0]
        return {
            "sectors": [{"key": k, "name": n, "symbols": counts.get(k, 0)} for k, n in sectors.SECTORS.items()],
            "unassigned": unassigned,
        }

    @app.get("/v1/symbols/{symbol}", tags=["symbols"], dependencies=read)
    def get_symbol(symbol: str, conn: Conn):
        """Look up by ticker or by id (the symbolId used in the UML designs)."""
        return _symbol_json(find_symbol(conn, symbol))

    @app.get("/v1/bars/{symbol}", tags=["bars"], dependencies=read)
    def bars(
        symbol: str,
        conn: Conn,
        timeframe: Timeframe = Timeframe.DAY,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10000,
        session: Session = Session.ALL,
    ):
        """Chart data. 1Min/1Hour/1Day are stored; 1Week/1Month/1Year are built from 1Day.
        Times without a zone are New York time. `session=regular` drops pre/after-market bars."""
        row = find_symbol(conn, symbol)
        return {
            "symbol": row["ticker"],
            "timeframe": timeframe.value,
            "bars": [_bar_json(b) for b in load_bars(conn, row["id"], timeframe, start, end, limit, session)],
        }

    @app.get("/v1/bars", tags=["bars"], dependencies=read)
    def multi_bars(
        conn: Conn,
        symbols: str,
        timeframe: Timeframe = Timeframe.DAY,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
        session: Session = Session.ALL,
    ):
        """Bars for several comma-separated symbols (e.g. a watchlist or portfolio). `limit` is per symbol."""
        return {
            "timeframe": timeframe.value,
            "bars": {
                r["ticker"]: [_bar_json(b) for b in load_bars(conn, r["id"], timeframe, start, end, limit, session)]
                for r in symbol_rows(conn, symbols)
            },
        }

    def latest(conn, table: str, symbol_id: str, before_ts: int | None = None) -> sqlite3.Row | None:
        if before_ts is None:
            return conn.execute(
                f"SELECT ts, c, v FROM {table} WHERE symbol_id = ? ORDER BY ts DESC LIMIT 1", (symbol_id,)
            ).fetchone()
        return conn.execute(
            f"SELECT ts, c, v FROM {table} WHERE symbol_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (symbol_id, before_ts),
        ).fetchone()

    def latest_price(conn, symbol_id: str) -> sqlite3.Row | None:
        minute, daily = latest(conn, "bars_1min", symbol_id), latest(conn, "bars_1day", symbol_id)
        if minute and (not daily or minute["ts"] >= daily["ts"]):
            return minute
        return daily

    @app.get("/v1/snapshots", tags=["prices"], dependencies=read)
    def snapshots(conn: Conn, symbols: str | None = None):
        """Latest price and daily change per symbol (all symbols if `symbols` is omitted).
        Feeds position P&L, SymbolPriceAction (LAST_CLOSE / LATEST_BAR) and the DAILY_* ranking keys."""
        out = {}
        for r in symbol_rows(conn, symbols):
            last = latest_price(conn, r["id"])
            days = conn.execute(
                "SELECT ts, c, v FROM bars_1day WHERE symbol_id = ? ORDER BY ts DESC LIMIT 2", (r["id"],)
            ).fetchall()
            today = days[0] if days else None
            prev = days[1] if len(days) > 1 else None
            change = last["c"] - prev["c"] if last and prev else None
            out[r["ticker"]] = {
                "latestPrice": last["c"] if last else None,
                "latestTime": _iso(last["ts"]) if last else None,
                "dailyClose": today["c"] if today else None,
                "dailyDate": _iso(today["ts"])[:10] if today else None,
                "dailyVolume": today["v"] if today else None,
                "previousClose": prev["c"] if prev else None,
                "dailyChange": change,
                "dailyChangePercent": change / prev["c"] * 100 if change is not None and prev["c"] else None,
            }
        return {"snapshots": out}

    @app.get("/v1/price-change", tags=["prices"], dependencies=read)
    def price_change(
        conn: Conn,
        period_amount: Annotated[int, Query(alias="periodAmount", ge=1)],
        period_unit: Annotated[PeriodUnit, Query(alias="periodUnit")],
        symbols: str | None = None,
    ):
        """Price change over a period, from the latest price back periodAmount × periodUnit.
        Matches PriceChangeAction in FiltersPage.mmd (compare `changePercent` or `change`)."""
        span = _period(period_amount, period_unit)
        table = {"MINUTES": "bars_1min", "HOURS": "bars_1hour"}.get(period_unit.value, "bars_1day")
        if table == "bars_1min" and span > timedelta(days=settings.minute_history_days):
            raise HTTPException(400, f"MINUTES periods are limited to {settings.minute_history_days} days of minute data")
        out = {}
        for r in symbol_rows(conn, symbols):
            last = latest_price(conn, r["id"])
            base = latest(conn, table, r["id"], last["ts"] - int(span.total_seconds())) if last else None
            change = last["c"] - base["c"] if last and base else None
            out[r["ticker"]] = {
                "fromTime": _iso(base["ts"]) if base else None,
                "fromPrice": base["c"] if base else None,
                "toTime": _iso(last["ts"]) if last else None,
                "toPrice": last["c"] if last else None,
                "change": change,
                "changePercent": change / base["c"] * 100 if change is not None and base["c"] else None,
            }
        return {"periodAmount": period_amount, "periodUnit": period_unit.value, "changes": out}

    @app.get("/v1/calendar", tags=["calendar"], dependencies=read)
    def calendar(conn: Conn, start: date | None = None, end: date | None = None):
        """Trading days with session times (New York time). Matches TradingCalendar / SessionTimes in Scheduling.mmd;
        a date missing from the list is not a trading day."""
        start = start or datetime.now(sync.ET).date()
        end = end or start + timedelta(days=30)
        rows = conn.execute(
            "SELECT * FROM calendar WHERE date >= ? AND date <= ? ORDER BY date", (start.isoformat(), end.isoformat())
        )
        return {
            "days": [
                {
                    "date": r["date"],
                    "preMarketOpen": r["session_open"],
                    "regularOpen": r["open"],
                    "regularClose": r["close"],
                    "afterHoursClose": r["session_close"],
                    "isEarlyClose": not r["close"].startswith(f"{r['date']}T16:00"),
                }
                for r in rows
            ]
        }

    # ---------------------------------------------------------------- admin

    def run_in_background(name: str, fn: Callable[[], None]) -> None:
        def target():
            with sync.WRITE_LOCK:
                try:
                    fn()
                except Exception:
                    log.exception("%s failed", name)

        threading.Thread(target=target, name=name, daemon=True).start()

    @app.post("/admin/halal", tags=["admin"], dependencies=admin)
    def upload_halal(conn: Conn, file: Annotated[UploadFile, File()], force: bool = False):
        """Replace the halal DB. Newly compliant symbols are added and backfilled in the background;
        symbols no longer compliant are deleted together with their price history.
        `force=true` allows removing more than half of the current symbols."""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=settings.data_dir, suffix=".halal-upload")
        try:
            with os.fdopen(fd, "wb") as f:
                shutil.copyfileobj(file.file, f)
            client = client_factory(settings)
            if not sync.WRITE_LOCK.acquire(timeout=5):
                raise HTTPException(409, "a sync is running; try again in a few minutes")
            try:
                summary = sync.import_halal(conn, client, settings, tmp, force=force)
            except halal.HalalDbError as e:
                raise HTTPException(400, str(e))
            except SymbolSyncError as e:
                raise HTTPException(409, str(e))
            finally:
                sync.WRITE_LOCK.release()
        finally:
            os.remove(tmp)

        if summary["added"]:
            def backfill():
                bconn = db.connect(settings.db_path)
                try:
                    sync.sync_all_timeframes(bconn, client, settings, datetime.now(timezone.utc))
                finally:
                    bconn.close()

            run_in_background("halal-backfill", backfill)
        return {**summary, "backfillStarted": bool(summary["added"])}

    @app.post("/admin/sync", tags=["admin"], dependencies=admin)
    def trigger_sync(job: Annotated[str, Query(pattern="^(incremental|nightly)$")] = "incremental"):
        """Run a sync now in the background (normally the scheduler does this)."""
        if sync.WRITE_LOCK.locked():
            raise HTTPException(409, "a sync is already running")
        client = client_factory(settings)

        def run():
            conn = db.connect(settings.db_path)
            try:
                (sync.run_nightly if job == "nightly" else sync.run_incremental)(conn, client, settings)
            finally:
                conn.close()

        run_in_background(f"manual-{job}", run)
        return {"started": job}

    return app
