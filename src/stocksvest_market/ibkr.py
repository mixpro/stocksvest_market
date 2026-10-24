"""Live data from Interactive Brokers (IB Gateway / TWS API): quotes for the few symbols an order is about to use, and
real-time minute bars for watched symbols (the ones the app holds).

Read-only: the connection is opened with readonly=True on its own client id, so it can't place orders. It runs on an
asyncio loop in a background thread (ib_async needs one) and reconnects by itself, e.g. after the gateway's daily
restart. Quotes are taken from short-lived streaming subscriptions (the account's "US Equity and Options Add-On
Streaming Bundle"): subscribe, wait for bid/ask/last, unsubscribe, so no paid snapshots are used.

Live bars: each watched symbol gets the last 30 minutes of 1-minute history (covering the ~16 minutes Alpaca's free
SIP data lags) and then 5-second real-time bars, rolled up into minutes and written to bars_live_1min, where
api.latest picks them up, so prices and price changes of watched symbols are current. A watch expires unless the app
renews it.

When IBKR can't answer (gateway down, not subscribed, another live session holding the data), the symbol is simply
missing from the answer and api.quotes falls back to the delayed price.
"""

import asyncio
import logging
import math
import threading
import time
from datetime import datetime, timezone

from . import db
from .config import Settings

log = logging.getLogger(__name__)
# ib_async logs every account event at INFO (e.g. other programs' executions on connect); keep only problems.
logging.getLogger("ib_async").setLevel(logging.WARNING)

# Symbols subscribed at once: IBKR allows ~100 simultaneous market data lines per account. Used by: IbkrQuotes._fetch
CHUNK = 50
# IBKR messages that only report status. Used by: IbkrQuotes._on_error
INFO_CODES = {2104, 2106, 2107, 2108, 2158, 2172, 300}
# Minute history fetched when a symbol starts streaming: covers the delayed SIP gap. Used by: IbkrQuotes._reconcile
LIVE_BACKFILL = "1800 S"
# Live minutes kept in memory (rolling up 5-second bars) and in bars_live_1min. Used by: IbkrQuotes._flush, _write
LIVE_MEMORY_SECONDS = 2 * 3600
LIVE_KEEP_SECONDS = 2 * 86400


def _epoch(t) -> int | None:
    """Unix seconds of an IBKR bar time (an aware datetime), or None for anything else (e.g. a daily date).

    Args:
        t: the bar's date/time.

    Used by: IbkrQuotes._reconcile, _on_bar.
    """
    return int(t.timestamp()) if isinstance(t, datetime) else None


def _num(v) -> float | None:
    """An IBKR price as float; NaN and IBKR's "no value" (-1) become None.

    Args:
        v: the value from a Ticker.

    Used by: IbkrQuotes._fetch.
    """
    if v is None or (isinstance(v, float) and math.isnan(v)) or v == -1:
        return None
    return float(v)


def ib_symbol(ticker: str) -> str:
    """The IBKR symbol for a ticker: share classes use a space (BRK.B -> "BRK B").

    Args:
        ticker: the market service's ticker.

    Used by: IbkrQuotes._contract.
    """
    return ticker.replace(".", " ").replace("-", " ")


class IbkrQuotes:
    """Background IBKR connection that answers quotes(tickers) from any thread.

    Attributes:
        connected: whether the API connection is up.
        last_error: the last connection or market data problem (shown by GET /v1/status), or None.
        last_quote_at: when the last live quote arrived.
        last_bar_at: when the last real-time bar arrived.

    Used by: api.create_app (started in lifespan when IBKR_ENABLED), api.quotes, api.live_watch, api.status.
    """

    def __init__(self, settings: Settings, ib_factory=None):
        """Args:
            settings: ibkr_host, ibkr_port, ibkr_client_id.
            ib_factory: creates the ib_async.IB object (tests pass a fake); default ib_async.IB.
        """
        self.settings = settings
        self.ib_factory = ib_factory
        self.connected = False
        self.last_error: str | None = None
        self.last_quote_at: datetime | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ib = None
        self._contracts: dict[str, object] = {}
        self._lock: asyncio.Lock | None = None
        self.last_bar_at: datetime | None = None
        self._watch: dict[str, float] = {}  # ticker -> time.monotonic() the watch expires
        self._watch_lock = threading.Lock()
        self._streams: dict[str, object] = {}  # ticker -> its RealTimeBarList (None: IBKR doesn't know it)
        self._minutes: dict[tuple[str, int], list] = {}  # (ticker, minute ts) -> [o, h, l, c, v]
        self._dirty: set[tuple[str, int]] = set()  # minutes changed since the last write
        self._last_prune = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ibkr-quotes", daemon=True)

    # -- lifecycle

    def start(self) -> None:
        """Start the background thread (it connects and keeps reconnecting)."""
        self._thread.start()

    def stop(self) -> None:
        """Disconnect and stop the thread."""
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def _run(self) -> None:
        """Thread body: an event loop for ib_async."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._lock = asyncio.Lock()
        try:
            self._loop.run_until_complete(self._keep_connected())
        finally:
            self._loop.close()

    async def _keep_connected(self) -> None:
        """Connect, watch the connection, reconnect with backoff (5 s up to 60 s)."""
        if self.ib_factory is None:
            from ib_async import IB
            self.ib_factory = IB
        delay = 5
        while not self._stop.is_set():
            if self._ib is None or not self._ib.isConnected():
                self.connected = False
                try:
                    self._ib = self.ib_factory()
                    self._ib.errorEvent += self._on_error
                    await self._ib.connectAsync(self.settings.ibkr_host, self.settings.ibkr_port,
                                                clientId=self.settings.ibkr_client_id, readonly=True, timeout=15)
                    self._ib.reqMarketDataType(1)  # live only: never take IBKR's delayed data for live
                    self._streams = {}  # subscriptions don't survive a reconnect: start them again
                    self.connected = True
                    self.last_error = None
                    delay = 5
                    log.info("IBKR connected (%s:%s, client %s)", self.settings.ibkr_host, self.settings.ibkr_port,
                             self.settings.ibkr_client_id)
                except Exception as e:  # gateway down or restarting: try again later
                    self.last_error = f"connect: {e or type(e).__name__}"
                    log.warning("IBKR not reachable: %s", self.last_error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
            try:
                await self._reconcile()
                await self._flush()
            except Exception as e:  # live bars never take the connection down
                self.last_error = f"live bars: {e or type(e).__name__}"
                log.warning("IBKR live bars failed: %s", self.last_error)
            await asyncio.sleep(2)
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    def _on_error(self, req_id, code, message, contract) -> None:
        """Keep the last real problem (e.g. 10197 competing live session, 354 not subscribed) for /v1/status.

        Args:
            req_id: IBKR request id.
            code: IBKR message code.
            message: IBKR text.
            contract: the contract it is about, if any.
        """
        if code not in INFO_CODES:
            self.last_error = f"{code}: {message[:200]}"

    # -- quotes

    def quotes(self, tickers: list[str], timeout: float = 4.0) -> dict[str, dict]:
        """Live quotes for these tickers, from any thread. Missing tickers had no live quote in time.

        Args:
            tickers: the market service's tickers.
            timeout: seconds to wait for each chunk of subscriptions.

        Returns:
            ticker -> {bid, ask, last, time (ISO), marketDataType}; {} when not connected.

        Used by: api.quotes.
        """
        if not self.connected or self._loop is None or not tickers:
            return {}
        future = asyncio.run_coroutine_threadsafe(self._fetch(list(dict.fromkeys(tickers)), timeout), self._loop)
        try:
            return future.result(timeout * (len(tickers) // CHUNK + 1) + 15)
        except Exception as e:
            self.last_error = f"quotes: {e or type(e).__name__}"
            log.warning("IBKR quotes failed: %s", self.last_error)
            return {}

    async def _contract(self, ticker: str):
        """The qualified IBKR contract of a ticker (cached), or None if IBKR doesn't know it.

        Args:
            ticker: the market service's ticker.
        """
        if ticker not in self._contracts:
            from ib_async import Stock
            c = Stock(ib_symbol(ticker), "SMART", "USD")
            try:
                found = await self._ib.qualifyContractsAsync(c)
            except Exception:
                found = []
            self._contracts[ticker] = found[0] if found and getattr(found[0], "conId", 0) else None
        return self._contracts[ticker]

    async def _fetch(self, tickers: list[str], timeout: float) -> dict[str, dict]:
        """Subscribe in chunks, wait until every symbol has a bid/ask or last price (or the timeout), unsubscribe.

        Args:
            tickers: unique tickers.
            timeout: seconds per chunk.
        """
        out: dict[str, dict] = {}
        async with self._lock:  # one fetch at a time keeps us under the market data line limit
            for i in range(0, len(tickers), CHUNK):
                chunk = []
                for t in tickers[i:i + CHUNK]:
                    c = await self._contract(t)
                    if c is not None:
                        chunk.append((t, c, self._ib.reqMktData(c, "", False, False)))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while loop.time() < deadline:
                    if all(_num(k.bid) is not None and _num(k.ask) is not None or _num(k.last) is not None
                           for _, _, k in chunk):
                        break
                    await asyncio.sleep(0.1)
                for t, c, k in chunk:
                    self._ib.cancelMktData(c)
                    bid, ask, last = _num(k.bid), _num(k.ask), _num(k.last)
                    if (bid is None or ask is None) and last is None:
                        continue
                    if getattr(k, "marketDataType", 1) not in (1, 2):  # 3/4 = delayed: not a live quote
                        continue
                    at = k.time or datetime.now(timezone.utc)
                    out[t] = {"bid": bid, "ask": ask, "last": last, "time": at.isoformat(timespec="seconds"),
                              "marketDataType": k.marketDataType}
        if out:
            self.last_quote_at = datetime.now(timezone.utc)
        return out

    # -- live bars

    def watch(self, tickers: list[str], ttl_seconds: int = 600) -> dict:
        """Stream real-time bars for these tickers until the watch expires (renew it to keep it), from any thread.
        Beyond ibkr_live_bars_max, the most recently renewed watches win.

        Args:
            tickers: the market service's tickers.
            ttl_seconds: how long the watch lasts without renewal.

        Returns:
            {"watching": tickers that stream, "overLimit": watched tickers left out by the limit}.

        Used by: api.live_watch.
        """
        now = time.monotonic()
        with self._watch_lock:
            for t in tickers:
                self._watch[t] = now + ttl_seconds
        ranked = self._ranked()
        limit = self.settings.ibkr_live_bars_max
        return {"watching": sorted(ranked[:limit]), "overLimit": sorted(ranked[limit:])}

    def _ranked(self) -> list[str]:
        """Unexpired watched tickers, most recently renewed first (expired ones are dropped).

        Used by: watch, _reconcile, live_status.
        """
        now = time.monotonic()
        with self._watch_lock:
            for t, expires in list(self._watch.items()):
                if expires <= now:
                    del self._watch[t]
            return sorted(self._watch, key=lambda t: (-self._watch[t], t))

    def live_status(self) -> dict:
        """Live bars for GET /v1/live and /v1/status: watched and streaming counts, time of the last bar.

        Used by: api.live_bars_status.
        """
        ranked = self._ranked()
        return {"watching": len(ranked[:self.settings.ibkr_live_bars_max]),
                "streaming": sum(1 for b in self._streams.values() if b is not None),
                "overLimit": max(0, len(ranked) - self.settings.ibkr_live_bars_max),
                "lastBarAt": self.last_bar_at.isoformat(timespec="seconds") if self.last_bar_at else None}

    async def _reconcile(self) -> None:
        """Start streams for newly watched tickers (after a 30-minute minute backfill) and stop expired ones.

        Used by: _keep_connected (every pass while connected).
        """
        want = set(self._ranked()[:self.settings.ibkr_live_bars_max])
        for t in [t for t in self._streams if t not in want]:
            bars = self._streams.pop(t)
            if bars is not None:
                self._ib.cancelRealTimeBars(bars)
        for t in sorted(want - set(self._streams)):
            c = await self._contract(t)
            if c is None:
                self._streams[t] = None  # IBKR doesn't know it: don't ask again until it is watched anew
                continue
            try:
                history = await self._ib.reqHistoricalDataAsync(c, "", LIVE_BACKFILL, "1 min", "TRADES", useRTH=False,
                                                                timeout=20)
            except Exception as e:
                history = []
                self.last_error = f"history {t}: {e or type(e).__name__}"
            for b in history or []:
                ts = _epoch(b.date)
                if ts is not None:
                    self._minutes[(t, ts)] = [b.open, b.high, b.low, b.close, float(b.volume or 0)]
                    self._dirty.add((t, ts))
            bars = self._ib.reqRealTimeBars(c, 5, "TRADES", False)
            bars.updateEvent += lambda rows, has_new, t=t: self._on_bar(t, rows, has_new)
            self._streams[t] = bars

    def _on_bar(self, ticker: str, rows, has_new: bool) -> None:
        """Roll a new 5-second bar into its minute.

        Args:
            ticker: the stream's ticker.
            rows: the stream's RealTimeBarList (newest last).
            has_new: whether a new bar arrived.

        Used by: the stream's updateEvent (set in _reconcile).
        """
        if not has_new or not rows:
            return
        b = rows[-1]
        ts = _epoch(b.time)
        close = _num(b.close)
        if ts is None or close is None:
            return
        key = (ticker, ts - ts % 60)
        m = self._minutes.get(key)
        if m is None:
            self._minutes[key] = [b.open_, b.high, b.low, close, float(b.volume or 0)]
        else:
            m[1], m[2], m[3] = max(m[1], b.high), min(m[2], b.low), close
            m[4] += float(b.volume or 0)
        self._dirty.add(key)
        self.last_bar_at = datetime.now(timezone.utc)

    async def _flush(self) -> None:
        """Write the minutes changed since the last pass to bars_live_1min, off the event loop.

        Used by: _keep_connected (every pass while connected).
        """
        cutoff = int(time.time()) - LIVE_MEMORY_SECONDS
        for key in [k for k in self._minutes if k[1] < cutoff]:
            del self._minutes[key]
        rows = [(t, ts, *self._minutes[(t, ts)]) for t, ts in self._dirty if (t, ts) in self._minutes]
        self._dirty = set()
        if rows:
            prune = time.monotonic() - self._last_prune > 3600
            if prune:
                self._last_prune = time.monotonic()
            await asyncio.get_running_loop().run_in_executor(None, self._write, rows, prune)

    def _write(self, rows: list[tuple], prune: bool) -> None:
        """Upsert live minutes (ticker, ts, o, h, l, c, v) and, hourly, drop ones older than LIVE_KEEP_SECONDS.

        Args:
            rows: minutes to write.
            prune: also delete old live minutes.

        Used by: _flush (in a worker thread).
        """
        conn = db.connect(self.settings.db_path)
        try:
            tickers = sorted({r[0] for r in rows})
            marks = ",".join("?" * len(tickers))
            ids = {r["ticker"]: r["id"]
                   for r in conn.execute(f"SELECT id, ticker FROM symbols WHERE ticker IN ({marks})", tickers)}
            conn.executemany(f"INSERT OR REPLACE INTO {db.LIVE_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                             [(ids[t], ts, o, h, lo, c, v) for t, ts, o, h, lo, c, v in rows if t in ids])
            if prune:
                conn.execute(f"DELETE FROM {db.LIVE_TABLE} WHERE ts < ?", (int(time.time()) - LIVE_KEEP_SECONDS,))
            conn.commit()
        except Exception as e:
            self.last_error = f"live bars write: {e or type(e).__name__}"
            log.warning("writing live bars failed: %s", self.last_error)
        finally:
            conn.close()
