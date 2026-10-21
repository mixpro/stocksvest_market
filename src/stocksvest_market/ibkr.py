"""Live quotes from Interactive Brokers (IB Gateway / TWS API) for the few symbols an order is about to use.

Read-only: the connection is opened with readonly=True on its own client id, so it can't place orders. It runs on an
asyncio loop in a background thread (ib_async needs one) and reconnects by itself, e.g. after the gateway's daily
restart. Quotes are taken from short-lived streaming subscriptions (the account's "US Equity and Options Add-On
Streaming Bundle"): subscribe, wait for bid/ask/last, unsubscribe, so no paid snapshots are used.

When IBKR can't answer (gateway down, not subscribed, another live session holding the data), the symbol is simply
missing from the answer and api.quotes falls back to the delayed price.
"""

import asyncio
import logging
import math
import threading
from datetime import datetime, timezone

from .config import Settings

log = logging.getLogger(__name__)

# Symbols subscribed at once: IBKR allows ~100 simultaneous market data lines per account. Used by: IbkrQuotes._fetch
CHUNK = 50
# IBKR messages that only report status. Used by: IbkrQuotes._on_error
INFO_CODES = {2104, 2106, 2107, 2108, 2158, 2172, 300}


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

    Used by: api.create_app (started in lifespan when IBKR_ENABLED), api.quotes, api.status.
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
