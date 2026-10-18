"""Alpaca adapter (Trading API v2). Paper: paper-api.alpaca.markets; live: api.alpaca.markets."""

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import requests

from .base import (
    CANCELLED,
    EXPIRED,
    FILLED,
    PARTIALLY_FILLED,
    REJECTED,
    SUBMITTED,
    AccountSnapshot,
    BrokerCapabilities,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerPosition,
)

log = logging.getLogger(__name__)

# Used by: AlpacaBroker.__init__
BASE_URLS = {"PAPER": "https://paper-api.alpaca.markets", "LIVE": "https://api.alpaca.markets"}

# Alpaca order status -> normalized status (unknown ones count as SUBMITTED). Used by: to_order
STATUS_MAP = {
    "new": SUBMITTED, "accepted": SUBMITTED, "pending_new": SUBMITTED, "accepted_for_bidding": SUBMITTED,
    "calculated": SUBMITTED, "done_for_day": SUBMITTED, "pending_cancel": SUBMITTED, "pending_replace": SUBMITTED,
    "stopped": SUBMITTED, "suspended": SUBMITTED,
    "partially_filled": PARTIALLY_FILLED,
    "filled": FILLED,
    "canceled": CANCELLED, "replaced": CANCELLED,
    "expired": EXPIRED,
    "rejected": REJECTED,
}

# What Alpaca supports. Used by: AlpacaBroker.capabilities
CAPABILITIES = BrokerCapabilities(
    order_types=frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"}),
    time_in_force=frozenset({"DAY", "GTC", "OPG", "CLS", "IOC", "FOK"}),  # no GTD
    fractional_shares=True,
    extended_hours=True,  # LIMIT + DAY only
    short_selling=True,
    max_orders_per_minute=180,
)


def _dec(v) -> Decimal | None:
    """An Alpaca number (string) as Decimal; None for null or "".

    Args:
        v: the value.
    """
    return Decimal(str(v)) if v not in (None, "") else None


def _time(v) -> datetime | None:
    """An Alpaca timestamp ("...Z") as an aware datetime, or None.

    Args:
        v: the value.
    """
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def to_order(o: dict) -> BrokerOrder:
    """An Alpaca order as a BrokerOrder.

    Args:
        o: the order JSON from the Trading API.

    Used by: AlpacaBroker.submit_order, get_order, get_order_by_client_id.
    """
    return BrokerOrder(
        broker_order_id=o["id"],
        client_order_id=o.get("client_order_id") or "",
        symbol=o["symbol"],
        side=o["side"].upper(),
        type=(o.get("type") or o.get("order_type") or "").upper(),
        status=STATUS_MAP.get(o["status"], SUBMITTED),
        raw_status=o["status"],
        quantity=_dec(o.get("qty")) or Decimal(0),
        filled_quantity=_dec(o.get("filled_qty")) or Decimal(0),
        average_fill_price=_dec(o.get("filled_avg_price")),
        reject_reason=o.get("reject_reason"),
        submitted_at=_time(o.get("submitted_at")),
        updated_at=_time(o.get("updated_at")),
    )


class AlpacaBroker:
    """Broker adapter for Alpaca's Trading API v2. Requests are retried on network errors, 429 and 5xx.

    Attributes:
        base: API base URL (paper or live).
        session: HTTP session carrying the key headers.

    Used by: registry.make_broker.
    """
    def __init__(self, key: str, secret: str, environment: str = "PAPER", session: requests.Session | None = None):
        """Args:
            key: API key id.
            secret: API secret.
            environment: PAPER or LIVE.
            session: HTTP session to use (tests pass a fake); default a new one.

        Raises:
            BrokerError: unknown environment.
        """
        if environment not in BASE_URLS:
            raise BrokerError(f"unknown Alpaca environment {environment}")
        self.base = BASE_URLS[environment]
        self.session = session or requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        """One API request with up to 4 attempts (back-off, Retry-After honored).

        Args:
            method: HTTP method.
            path: API path, e.g. /v2/orders.
            params: query parameters.
            body: JSON body.

        Returns:
            The JSON answer ({} when empty), or None for 404.

        Raises:
            BrokerError: refused (4xx, retryable=False) or still failing after the retries (retryable=True).
        """
        for attempt in range(4):
            try:
                r = self.session.request(method, f"{self.base}{path}", params=params, json=body, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    raise BrokerError(f"Alpaca unreachable: {e}", retryable=True) from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise BrokerError(f"Alpaca {r.status_code}: {r.text[:200]}", retryable=True)
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                try:
                    message = r.json().get("message", r.text)
                except ValueError:
                    message = r.text
                raise BrokerError(f"Alpaca refused ({r.status_code}): {message}")
            return r.json() if r.content else {}
        raise BrokerError("Alpaca: giving up", retryable=True)

    def capabilities(self) -> BrokerCapabilities:
        """CAPABILITIES."""
        return CAPABILITIES

    def account_snapshot(self) -> AccountSnapshot:
        """Balances from GET /v2/account."""
        a = self._call("GET", "/v2/account")
        return AccountSnapshot(cash=_dec(a["cash"]), buying_power=_dec(a["buying_power"]), equity=_dec(a["equity"]),
                               currency=a.get("currency", "USD"), at=datetime.now().astimezone(), raw=a)

    def submit_order(self, req: BrokerOrderRequest) -> BrokerOrder:
        """POST /v2/orders. A duplicate client_order_id returns the order already there (safe to resend).

        Args:
            req: the order.
        """
        body = {
            "client_order_id": req.client_order_id,
            "symbol": req.symbol,
            "qty": str(req.quantity),
            "side": req.side.lower(),
            "type": req.type.lower(),
            "time_in_force": req.time_in_force.lower(),
            "extended_hours": req.extended_hours,
        }
        for key, value in (("limit_price", req.limit_price), ("stop_price", req.stop_price),
                           ("trail_percent", req.trail_percent)):
            if value is not None:
                body[key] = str(value)
        try:
            return to_order(self._call("POST", "/v2/orders", body=body))
        except BrokerError as e:
            # Submitting the same client_order_id twice: return the order that already exists.
            if "client_order_id" in str(e) and "unique" in str(e):
                existing = self.get_order_by_client_id(req.client_order_id)
                if existing:
                    return existing
            raise

    def cancel_order(self, broker_order_id: str) -> None:
        """DELETE /v2/orders/{id} (already gone = nothing to do).

        Args:
            broker_order_id: Alpaca's order id.
        """
        self._call("DELETE", f"/v2/orders/{broker_order_id}")

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        """GET /v2/orders/{id}.

        Args:
            broker_order_id: Alpaca's order id.

        Raises:
            BrokerError: not found.
        """
        o = self._call("GET", f"/v2/orders/{broker_order_id}")
        if o is None:
            raise BrokerError(f"order {broker_order_id} not found at Alpaca")
        return to_order(o)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """GET /v2/orders:by_client_order_id; None when Alpaca never received it.

        Args:
            client_order_id: the app's id of the order.
        """
        o = self._call("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        return to_order(o) if o else None

    def list_fills(self, since: datetime | None) -> list[BrokerFill]:
        """FILL activities, oldest first, all pages (Alpaca reports no fees for stocks: fee 0).

        Args:
            since: only fills after this time; None = all.
        """
        params = {"direction": "asc", "page_size": 100}
        if since:
            params["after"] = since.isoformat()
        fills = []
        while True:
            page = self._call("GET", "/v2/account/activities/FILL", params=params) or []
            for f in page:
                fills.append(BrokerFill(
                    broker_fill_id=f["id"], broker_order_id=f["order_id"], symbol=f["symbol"],
                    side="BUY" if f["side"] == "buy" else "SELL", quantity=_dec(f["qty"]), price=_dec(f["price"]),
                    fee=Decimal(0), executed_at=_time(f["transaction_time"])))
            if len(page) < params["page_size"]:
                return fills
            params["page_token"] = page[-1]["id"]

    def equity_history(self, period: str = "1M", timeframe: str = "1D") -> list[dict]:
        """The whole account's equity over time: [{at, equity, profitLoss}]. Leading zeros from before the account
        had money are skipped.

        Args:
            period: how far back, e.g. 1W, 1M, 3M, 1A.
            timeframe: 1Min, 5Min, 15Min, 1H or 1D.
        """
        h = self._call("GET", "/v2/account/portfolio/history", params={"period": period, "timeframe": timeframe}) or {}
        out = []
        for ts, equity, pnl in zip(h.get("timestamp") or [], h.get("equity") or [], h.get("profit_loss") or []):
            if equity is not None and (out or equity != 0):  # skip zeros from before the account existed
                out.append({"at": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "equity": float(equity), "profitLoss": float(pnl) if pnl is not None else None})
        return out

    def positions(self) -> list[BrokerPosition]:
        """Shares held, from GET /v2/positions."""
        return [BrokerPosition(symbol=p["symbol"], quantity=_dec(p["qty"]), average_cost=_dec(p["avg_entry_price"]),
                               market_value=_dec(p.get("market_value")))
                for p in self._call("GET", "/v2/positions") or []]
