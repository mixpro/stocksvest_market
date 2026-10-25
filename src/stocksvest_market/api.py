"""HTTP API. Read endpoints (/v1) need an API key; /admin endpoints need the admin key."""

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import aggregate, db, halal, sectors, sync
from .alpaca import AlpacaClient
from .config import Settings
from .scheduler import Scheduler
from .symbols import SymbolSyncError

log = logging.getLogger(__name__)

# Largest `limit` accepted by list and bar endpoints. Used by: list_symbols, bars, multi_bars.
MAX_LIMIT = 50000


class Timeframe(str, Enum):
    """Bar size requested by clients. 1Min/1Hour/1Day are stored (db.BAR_TABLES); 1Week/1Month/1Year are built
    from 1Day bars by aggregate.aggregate.

    Used by: bars, multi_bars (the `timeframe` query parameter), load_bars. The app service mirrors these values in
    its /v1/market/bars* endpoints and sample chart.
    """

    MIN = "1Min"
    HOUR = "1Hour"
    DAY = "1Day"
    WEEK = "1Week"
    MONTH = "1Month"
    YEAR = "1Year"


class Session(str, Enum):
    """Which part of the trading day intraday bars cover. ALL = pre-market + regular + after-hours;
    REGULAR = 09:30-16:00 New York (from the calendar, so early closes count). Ignored for daily and longer bars.

    Used by: bars, multi_bars (the `session` query parameter), load_bars.
    """

    ALL = "all"  # pre-market + regular + after-hours
    REGULAR = "regular"


class PeriodUnit(str, Enum):
    """Unit of the look-back in GET /v1/price-change.

    MINUTES / HOURS count market time only (the extended session, 04:00-20:00 New York on trading days), using
    minute / hourly bars. DAYS counts trading days (weekends and holidays skipped). WEEKS / MONTHS / YEARS are
    calendar periods (months 30.4375 days, years 365.25 days, see _period) on daily bars.

    Used by: price_change, _period. The app's filter action PRICE_CHANGE (TimePeriodUnit) sends these values.
    """

    MINUTES = "MINUTES"
    HOURS = "HOURS"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    MONTHS = "MONTHS"
    YEARS = "YEARS"


def _iso(ts: int) -> str:
    """Unix seconds -> "YYYY-MM-DDTHH:MM:SSZ" (UTC), the time format of every response.

    Args:
        ts: unix seconds.

    Used by: _bar_json, snapshots, price_change.
    """
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str | None, default: datetime) -> int:
    """Read a `start`/`end` query value.

    Args:
        value: a date ("2026-10-01", midnight New York) or datetime; without a zone it is New York time.
            Empty uses `default`.
        default: used when value is empty.

    Returns:
        Unix seconds.

    Raises:
        HTTPException: 400 for an unreadable value.

    Used by: load_bars (also to read calendar times).
    """
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
    """A stored or aggregated bar -> response shape {t, o, h, l, c, v, n, vw}.

    Args:
        r: dict with ts, o, h, l, c, v, n, vw.

    Used by: bars, multi_bars.
    """
    return {"t": _iso(r["ts"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"], "n": r["n"], "vw": r["vw"]}


def _symbol_json(r: sqlite3.Row) -> dict:
    """A symbols row -> response shape (id, ticker, name, exchange, assetType, sector, sic, sicDescription,
    halalCompliant).

    Args:
        r: row from the symbols table.

    Used by: list_symbols, get_symbol.
    """
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
    """A look-back period as a timedelta (for DAYS this is only used for the MINUTES limit check; DAYS are counted
    in trading days by _trading_days_back).

    Args:
        amount: how many units (>= 1).
        unit: the PeriodUnit.

    Used by: price_change.
    """
    days = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30.4375, "YEARS": 365.25}
    if unit == PeriodUnit.MINUTES:
        return timedelta(minutes=amount)
    if unit == PeriodUnit.HOURS:
        return timedelta(hours=amount)
    return timedelta(days=amount * days[unit.value])


def _trading_days_back(days_desc: list[str], last_date: str, amount: int) -> str | None:
    """The trading day whose close starts a look-back of `amount` trading days ending on `last_date`.

    The window counts the latest trading day on or before last_date as day 1. Example: on Monday, 1 day starts from
    Friday's close (Monday's move) and 2 days from Thursday's close (Friday + Monday).

    Args:
        days_desc: trading days ("YYYY-MM-DD"), newest first.
        last_date: New York date of the latest price.
        amount: trading days to look back (>= 1).

    Returns:
        The start day, or None when the calendar doesn't reach that far back.

    Used by: create_app.price_change (DAYS), create_app.snapshots (previous close).
    """
    prior = [d for d in days_desc if d <= last_date]
    return prior[amount] if len(prior) > amount else None


def _market_time_back(sessions_desc: list[tuple[datetime, datetime]], end: datetime, span: timedelta) -> datetime | None:
    """Go back `span` of market time from `end`, skipping the hours the market is closed (nights, weekends, holidays).

    Args:
        sessions_desc: (extended session open, extended session close) of trading days, newest first.
        end: the time of the latest price.
        span: market time to go back.

    Returns:
        The start time, or None when the calendar doesn't reach that far back.

    Used by: create_app.price_change (MINUTES, HOURS).
    """
    left = span
    for open_, close in sessions_desc:
        if open_ >= end:
            continue
        available = min(close, end) - open_
        if left <= available:
            return min(close, end) - left
        left -= available
    return None


def create_app(settings: Settings | None = None, client_factory: Callable[[Settings], AlpacaClient] = AlpacaClient,
               quote_source=None):
    """Build the market service: endpoints, auth, and (when enabled) the background sync scheduler and the IBKR
    live quote connection.

    Args:
        settings: service settings; default reads the environment.
        client_factory: builds the Alpaca client for admin jobs (tests pass a fake).
        quote_source: live quotes (an object with quotes(tickers), connected, last_error); default IbkrQuotes when
            IBKR_ENABLED, else none (tests pass a fake).

    Returns:
        The FastAPI app.

    Used by: __main__.main (serve), tests.
    """
    settings = settings or Settings()
    owns_quotes = quote_source is None and settings.ibkr_enabled
    if owns_quotes:
        from .ibkr import IbkrQuotes
        quote_source = IbkrQuotes(settings)
    state: dict = {"scheduler": None, "quotes": quote_source}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Startup: create/upgrade the DB, start the scheduler (RUN_SCHEDULER) and the IBKR connection
        (IBKR_ENABLED). Shutdown: stop both.

        Args:
            app: the FastAPI app.
        """
        conn = db.connect(settings.db_path)
        db.init(conn)
        conn.close()
        if settings.run_scheduler:
            state["scheduler"] = Scheduler(settings)
            state["scheduler"].start()
        if owns_quotes:
            quote_source.start()
        yield
        if state["scheduler"]:
            state["scheduler"].stop()
        if owns_quotes:
            quote_source.stop()

    app = FastAPI(
        title="StocksVest Market",
        description="Halal-compliant NYSE/NASDAQ symbols and price history, synced from Alpaca.",
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_conn() -> Iterator[sqlite3.Connection]:
        """Dependency: one DB connection per request, closed afterwards."""
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        """Dependency for /v1: accept any API_KEYS value or the admin key.

        Args:
            x_api_key: the X-API-Key header.

        Raises:
            HTTPException: 401 when missing or wrong.
        """
        if not x_api_key or x_api_key not in settings.api_keys + ([settings.admin_key] if settings.admin_key else []):
            raise HTTPException(401, "missing or invalid X-API-Key")

    def require_admin(x_api_key: Annotated[str | None, Header()] = None) -> None:
        """Dependency for /admin: accept only ADMIN_KEY (refuses everything when it is not set).

        Args:
            x_api_key: the X-API-Key header.

        Raises:
            HTTPException: 401 when missing or wrong.
        """
        if not settings.admin_key or x_api_key != settings.admin_key:
            raise HTTPException(401, "missing or invalid admin X-API-Key")

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    read = [Depends(require_key)]
    admin = [Depends(require_admin)]

    def find_symbol(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row:
        """Look a symbol up by ticker (any case) or id.

        Args:
            conn: market DB connection.
            symbol: ticker or Alpaca asset id.

        Raises:
            HTTPException: 404 when it isn't in the halal universe.

        Used by: get_symbol, bars, symbol_rows.
        """
        row = conn.execute("SELECT * FROM symbols WHERE ticker = ? OR id = ?", (symbol.upper(), symbol)).fetchone()
        if not row:
            raise HTTPException(404, f"unknown symbol '{symbol}' (not listed, or not halal compliant)")
        return row

    def symbol_rows(conn: sqlite3.Connection, symbols: str | None) -> list[sqlite3.Row]:
        """Rows for a comma-separated list of tickers/ids, or every symbol when empty.

        Args:
            conn: market DB connection.
            symbols: "AAPL,MSFT" or None.

        Raises:
            HTTPException: 404 for any unknown symbol.

        Used by: multi_bars, snapshots, price_change, volume.
        """
        if not symbols:
            return conn.execute("SELECT * FROM symbols ORDER BY ticker").fetchall()
        return [find_symbol(conn, s.strip()) for s in symbols.split(",") if s.strip()]

    def load_bars(
        conn, symbol_id: str, timeframe: Timeframe, start: str | None, end: str | None, limit: int, session: Session
    ) -> list[dict]:
        """Bars of one symbol, oldest first.

        Args:
            conn: market DB connection.
            symbol_id: the symbol's id.
            timeframe: requested size; week/month/year are aggregated from daily bars.
            start: first bar time (see _parse_time); default 10 years ago.
            end: last bar time; default now.
            limit: keep only the most recent `limit` bars.
            session: REGULAR drops pre/after-market intraday bars.

        Used by: bars, multi_bars.
        """
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
                """True if a bar starting at `ts` (unix seconds) is inside that day's regular session.

                Args:
                    ts: bar start.
                """
                day = datetime.fromtimestamp(ts, timezone.utc).astimezone(sync.ET).date().isoformat()
                return day in hours and hours[day][0] <= ts < hours[day][1]

            rows = [r for r in rows if regular(r["ts"])]
        if stored != timeframe.value:
            rows = aggregate.aggregate(rows, timeframe.value)
        return rows[-limit:]

    # ---------------------------------------------------------------- public

    @app.get("/health", tags=["status"])
    def health():
        """Liveness check; no key needed."""
        return {"ok": True}

    @app.get("/v1/status", tags=["status"], dependencies=read)
    def status(conn: Conn):
        """Symbol and bar counts, last sync times, last halal import, the running job and the last sync error.
        \f
        Args:
            conn: market DB connection (one per request, injected).
        """
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
            "liveQuotes": quotes_status(),
            "liveBars": live_bars_status(),
        }

    def live_bars_status() -> dict:
        """Real-time IBKR bars: enabled, how many symbols are watched / streaming, time of the last bar.

        Used by: status, live, live_watch.
        """
        q = state["quotes"]
        if q is None or not hasattr(q, "live_status"):
            return {"enabled": False, "watching": 0, "streaming": 0, "overLimit": 0, "lastBarAt": None}
        return {"enabled": True, **q.live_status()}

    class WatchIn(BaseModel):
        """Body of PUT /v1/live/watch. Used by: live_watch."""
        symbols: list[str] = Field(max_length=500)
        ttlMinutes: int = Field(default=10, ge=1, le=120)

    @app.put("/v1/live/watch", tags=["prices"], dependencies=read)
    def live_watch(conn: Conn, body: WatchIn):
        """Stream IBKR real-time bars for these symbols (e.g. a portfolio's holdings) for `ttlMinutes`; send the list
        again before it runs out to keep them. While watched, a symbol's latest price, snapshot and price changes use
        real-time minutes from IBKR on top of the SIP minute bars. Unknown symbols are listed, not refused.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            body: WatchIn.
        """
        known = {r["ticker"] for r in conn.execute("SELECT ticker FROM symbols")}
        asked = list(dict.fromkeys(s.strip().upper() for s in body.symbols if s.strip()))
        found = [t for t in asked if t in known]
        unknown = sorted(set(asked) - set(found))
        q = state["quotes"]
        if q is None or not hasattr(q, "watch"):
            return {"watching": [], "overLimit": [], "unknown": unknown, "liveBars": live_bars_status()}
        return {**q.watch(found, body.ttlMinutes * 60), "unknown": unknown, "liveBars": live_bars_status()}

    @app.get("/v1/live", tags=["prices"], dependencies=read)
    def live():
        """Whether real-time IBKR bars are running: watched and streaming symbols, time of the last bar."""
        return live_bars_status()

    def quotes_status() -> dict:
        """The live quote source's state: enabled, connected, last error, time of the last live quote.

        Used by: status, quotes.
        """
        q = state["quotes"]
        if q is None:
            return {"enabled": False, "connected": False, "lastError": None, "lastQuoteAt": None}
        last = getattr(q, "last_quote_at", None)
        return {"enabled": True, "connected": bool(q.connected), "lastError": q.last_error,
                "lastQuoteAt": last.isoformat(timespec="seconds") if last else None}

    @app.get("/v1/quotes", tags=["prices"], dependencies=read)
    def quotes(conn: Conn, symbols: Annotated[str, Query(description="Comma-separated tickers/ids, up to 100")]):
        """Live price per symbol right now: bid, ask and last from IBKR (`source: IBKR`), or, when IBKR can't
        answer, the newest minute bar (`source: DELAYED`; real-time SIP on the paid plan, about 16 minutes old on the
        free one). `price` is the bid/ask midpoint (or the last trade); `ageSeconds` says how old it is.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            symbols: comma-separated tickers or ids (at most 100).
        """
        rows = symbol_rows(conn, symbols)
        if len(rows) > 100:
            raise HTTPException(400, "at most 100 symbols per request")
        source = state["quotes"]
        live = source.quotes([r["ticker"] for r in rows]) if source is not None else {}
        now = datetime.now(timezone.utc)
        out = {}
        for r in rows:
            q = live.get(r["ticker"])
            if q:
                bid, ask, last = q.get("bid"), q.get("ask"), q.get("last")
                at = datetime.fromisoformat(q["time"])
                out[r["ticker"]] = {"source": "IBKR", "bid": bid, "ask": ask, "last": last,
                                    "price": round((bid + ask) / 2, 4) if bid is not None and ask is not None else last,
                                    "time": _iso(int(at.timestamp())),
                                    "ageSeconds": max(0, int((now - at).total_seconds()))}
                continue
            bar = latest_price(conn, r["id"])
            out[r["ticker"]] = {"source": "DELAYED", "bid": None, "ask": None,
                                "last": bar["c"] if bar else None, "price": bar["c"] if bar else None,
                                "time": _iso(bar["ts"]) if bar else None,
                                "ageSeconds": int(now.timestamp()) - bar["ts"] if bar else None}
        return {"quotes": out, "liveQuotes": quotes_status()}

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
        """The halal universe. Use as the UNIVERSE input of a filter.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            search: ticker prefix or part of the name.
            exchange: "NASDAQ", "NYSE", ...
            asset_type: "COMMON_STOCK" or "ADR".
            sector: comma-separated keys of sectors.SECTORS.
            limit: page size (1..MAX_LIMIT).
            offset: rows to skip.
        """
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
        """Sector keys and names with the number of halal symbols in each (SIC-based, approximate GICS).
        \f
        Args:
            conn: market DB connection (one per request, injected).
        """
        counts = dict(conn.execute("SELECT sector, COUNT(*) FROM symbols WHERE sector IS NOT NULL GROUP BY sector"))
        unassigned = conn.execute("SELECT COUNT(*) FROM symbols WHERE sector IS NULL").fetchone()[0]
        return {
            "sectors": [{"key": k, "name": n, "symbols": counts.get(k, 0)} for k, n in sectors.SECTORS.items()],
            "unassigned": unassigned,
        }

    @app.get("/v1/symbols/{symbol}", tags=["symbols"], dependencies=read)
    def get_symbol(symbol: str, conn: Conn):
        """Look up by ticker or by id (the symbolId used in the UML designs).
        \f
        Args:
            symbol: ticker or id.
            conn: market DB connection (one per request, injected).
        """
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
        Times without a zone are New York time. `session=regular` drops pre/after-market bars.
        \f
        Args:
            symbol: ticker or id.
            conn: market DB connection (one per request, injected).
            timeframe: bar size.
            start: first bar time.
            end: last bar time.
            limit: most recent bars to return.
            session: all or regular.
        """
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
        """Bars for several comma-separated symbols (e.g. a watchlist or portfolio). `limit` is per symbol.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            symbols: "AAPL,MSFT".
            timeframe: bar size.
            start: first bar time.
            end: last bar time.
            limit: most recent bars per symbol.
            session: all or regular.
        """
        return {
            "timeframe": timeframe.value,
            "bars": {
                r["ticker"]: [_bar_json(b) for b in load_bars(conn, r["id"], timeframe, start, end, limit, session)]
                for r in symbol_rows(conn, symbols)
            },
        }

    def latest(conn, table: str, symbol_id: str, before_ts: int | None = None) -> sqlite3.Row | None:
        """The newest bar (ts, c, v) of a symbol in a table, optionally at or before a time. For minutes, IBKR's
        real-time bars (watched symbols) count too, so the newest minute is current even between syncs.

        Args:
            conn: market DB connection.
            table: "bars_1min", "bars_1hour" or "bars_1day".
            symbol_id: the symbol's id.
            before_ts: unix seconds; None = the newest bar.

        Used by: latest_price, snapshots, price_change.
        """
        best = None
        for t in (table, db.LIVE_TABLE) if table == "bars_1min" else (table,):
            if before_ts is None:
                row = conn.execute(
                    f"SELECT ts, c, v FROM {t} WHERE symbol_id = ? ORDER BY ts DESC LIMIT 1", (symbol_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT ts, c, v FROM {t} WHERE symbol_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                    (symbol_id, before_ts),
                ).fetchone()
            if row is not None and (best is None or row["ts"] > best["ts"]):
                best = row
        return best

    def latest_price(conn, symbol_id: str) -> sqlite3.Row | None:
        """The newest price: the latest minute bar, or the latest daily bar if it is newer (or no minute bars).

        Args:
            conn: market DB connection.
            symbol_id: the symbol's id.

        Used by: snapshots, price_change.
        """
        minute, daily = latest(conn, "bars_1min", symbol_id), latest(conn, "bars_1day", symbol_id)
        if minute and (not daily or minute["ts"] >= daily["ts"]):
            return minute
        return daily

    @app.get("/v1/snapshots", tags=["prices"], dependencies=read)
    def snapshots(conn: Conn, symbols: str | None = None):
        """Latest price and daily change per symbol (all symbols if `symbols` is omitted).
        Feeds position P&L, SymbolPriceAction (LAST_CLOSE / LATEST_BAR) and the DAILY_* ranking keys.

        `previousClose` is the close of the trading day before the latest price's day, so the daily change is the
        move of that day only, also in pre-market before the day's daily bar exists.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            symbols: comma-separated tickers/ids, or None for all.
        """
        trading_days = [c["date"] for c in conn.execute("SELECT date FROM calendar ORDER BY date DESC")]
        out = {}
        for r in symbol_rows(conn, symbols):
            last = latest_price(conn, r["id"])
            days = conn.execute(
                "SELECT ts, c, v FROM bars_1day WHERE symbol_id = ? ORDER BY ts DESC LIMIT 2", (r["id"],)
            ).fetchall()
            today = days[0] if days else None
            prev = days[1] if len(days) > 1 else None  # without a calendar: the bar before the newest one
            last_date = datetime.fromtimestamp(last["ts"], sync.ET).date().isoformat() if last else None
            prev_day = _trading_days_back(trading_days, last_date, 1) if last_date else None
            if prev_day:  # the trading day before the latest price's day (daily bars start at midnight New York)
                prev = latest(conn, "bars_1day", r["id"],
                              int(datetime.combine(date.fromisoformat(prev_day), time(), sync.ET).timestamp()))
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
        Matches PriceChangeAction in FiltersPage.mmd (compare `changePercent` or `change`).

        Only time the market is open counts: DAYS are trading days (on Monday, 2 DAYS compares with Thursday's
        close), MINUTES / HOURS are market time within the extended session (04:00-20:00 New York).
        WEEKS / MONTHS / YEARS are calendar periods. `fromTime` is the bar the change starts from.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            period_amount: how many units (query: periodAmount).
            period_unit: the PeriodUnit (query: periodUnit).
            symbols: comma-separated tickers/ids, or None for all.
        """
        span = _period(period_amount, period_unit)
        table = {"MINUTES": "bars_1min", "HOURS": "bars_1hour"}.get(period_unit.value, "bars_1day")
        if table == "bars_1min" and span > timedelta(days=settings.minute_history_days):
            raise HTTPException(400, f"MINUTES periods are limited to {settings.minute_history_days} days of minute data")
        unit = period_unit.value
        calendar = conn.execute("SELECT date, session_open, session_close FROM calendar ORDER BY date DESC").fetchall()
        starts: dict[int, int | None] = {}  # latest price time -> look-back start (same for most symbols)

        def start_of(last_ts: int) -> int | None:
            """Unix time the look-back starts from (None when the calendar is too short), cached per time.

            Args:
                last_ts: unix time of the symbol's latest price.
            """
            if last_ts not in starts:
                end = datetime.fromtimestamp(last_ts, timezone.utc)
                if unit == "DAYS":
                    day = _trading_days_back([c["date"] for c in calendar], end.astimezone(sync.ET).date().isoformat(),
                                             period_amount)
                    # Daily bars start at midnight New York, so the start day's bar is at or before this time.
                    t = datetime.combine(date.fromisoformat(day), time(), sync.ET) if day else None
                elif unit in ("MINUTES", "HOURS"):
                    sessions = [(datetime.fromisoformat(c["session_open"]), datetime.fromisoformat(c["session_close"]))
                                for c in calendar]
                    t = _market_time_back(sessions, end, span)
                else:
                    t = end - span
                starts[last_ts] = int(t.timestamp()) if t else None
            return starts[last_ts]

        out = {}
        for r in symbol_rows(conn, symbols):
            last = latest_price(conn, r["id"])
            start = start_of(last["ts"]) if last else None
            base = latest(conn, table, r["id"], start) if start is not None else None
            if base is None and start is not None and table == "bars_1min":
                base = latest(conn, "bars_1hour", r["id"], start)  # older than the minute history kept
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

    @app.get("/v1/volume", tags=["prices"], dependencies=read)
    def volume(conn: Conn, days: Annotated[int, Query(ge=1, le=260)] = 20, symbols: str | None = None):
        """Average daily trading over the last `days` completed trading days (today's is left out until its
        extended session has ended). A trading day without a daily bar counts as 0, so stocks that often don't
        trade at all average low. Shares and dollars (volume x volume-weighted price, else the close).
        \f
        Args:
            conn: market DB connection (one per request, injected).
            days: how many trading days.
            symbols: comma-separated tickers/ids, or None for all.
        """
        now = datetime.now(timezone.utc)
        done = [c["date"] for c in conn.execute("SELECT date, session_close FROM calendar ORDER BY date DESC")
                if datetime.fromisoformat(c["session_close"]) <= now][:days]
        rows = symbol_rows(conn, symbols)
        if len(done) < days:  # the calendar doesn't reach that far back
            return {"days": days, "from": None, "to": None,
                    "volumes": {r["ticker"]: {"averageVolume": None, "averageDollarVolume": None, "daysTraded": None}
                                for r in rows}}
        # Daily bars start at midnight New York: from the first day's midnight to the day after the last one.
        start = int(datetime.combine(date.fromisoformat(done[-1]), time(), sync.ET).timestamp())
        end = int((datetime.combine(date.fromisoformat(done[0]), time(), sync.ET) + timedelta(days=1)).timestamp())
        sums = {r["symbol_id"]: r for r in conn.execute(
            "SELECT symbol_id, SUM(v) AS shares, SUM(v * COALESCE(vw, c)) AS dollars, COUNT(*) AS traded "
            "FROM bars_1day WHERE ts >= ? AND ts < ? AND v > 0 GROUP BY symbol_id", (start, end))}
        out = {}
        for r in rows:
            s = sums.get(r["id"])
            out[r["ticker"]] = {"averageVolume": s["shares"] / days if s else 0.0,
                                "averageDollarVolume": s["dollars"] / days if s else 0.0,
                                "daysTraded": s["traded"] if s else 0}
        return {"days": days, "from": done[-1], "to": done[0], "volumes": out}

    @app.get("/v1/calendar", tags=["calendar"], dependencies=read)
    def calendar(conn: Conn, start: date | None = None, end: date | None = None):
        """Trading days with session times (New York time). Matches TradingCalendar / SessionTimes in Scheduling.mmd;
        a date missing from the list is not a trading day.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            start: first date; default today (New York).
            end: last date; default start + 30 days.
        """
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
        """Run `fn` in a daemon thread under sync.WRITE_LOCK; errors are logged.

        Args:
            name: thread name (shows in logs).
            fn: the job.

        Used by: upload_halal (backfill), trigger_sync.
        """
        def target():
            """Thread body: run the job under the write lock."""
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
        `force=true` allows removing more than half of the current symbols.
        \f
        Args:
            conn: market DB connection (one per request, injected).
            file: the uploaded SQLite halal DB.
            force: see symbols.reconcile.

        Raises:
            HTTPException: 400 invalid file, 409 a sync is running or the change was refused.
        """
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
                """Download the history of the newly added symbols."""
                bconn = db.connect(settings.db_path)
                try:
                    sync.sync_all_timeframes(bconn, client, settings, datetime.now(timezone.utc))
                finally:
                    bconn.close()

            run_in_background("halal-backfill", backfill)
        return {**summary, "backfillStarted": bool(summary["added"])}

    @app.post("/admin/sync", tags=["admin"], dependencies=admin)
    def trigger_sync(job: Annotated[str, Query(pattern="^(incremental|nightly)$")] = "incremental"):
        """Run a sync now in the background (normally the scheduler does this).
        \f
        Args:
            job: "incremental" or "nightly".

        Raises:
            HTTPException: 409 when a sync is already running.
        """
        if sync.WRITE_LOCK.locked():
            raise HTTPException(409, "a sync is already running")
        client = client_factory(settings)

        def run():
            """Run the requested job on its own connection."""
            conn = db.connect(settings.db_path)
            try:
                (sync.run_nightly if job == "nightly" else sync.run_incremental)(conn, client, settings)
            finally:
                conn.close()

        run_in_background(f"manual-{job}", run)
        return {"started": job}

    return app
