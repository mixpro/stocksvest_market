"""Alpaca adapter (Trading API v2). Paper: paper-api.alpaca.markets; live: api.alpaca.markets."""

import logging
import time
from datetime import datetime
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

BASE_URLS = {"PAPER": "https://paper-api.alpaca.markets", "LIVE": "https://api.alpaca.markets"}

# Alpaca order status -> normalized status.
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

CAPABILITIES = BrokerCapabilities(
    order_types=frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"}),
    time_in_force=frozenset({"DAY", "GTC", "OPG", "CLS", "IOC", "FOK"}),  # no GTD
    fractional_shares=True,
    extended_hours=True,  # LIMIT + DAY only
    short_selling=True,
    max_orders_per_minute=180,
)


def _dec(v) -> Decimal | None:
    return Decimal(str(v)) if v not in (None, "") else None


def _time(v) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def to_order(o: dict) -> BrokerOrder:
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
    def __init__(self, key: str, secret: str, environment: str = "PAPER", session: requests.Session | None = None):
        if environment not in BASE_URLS:
            raise BrokerError(f"unknown Alpaca environment {environment}")
        self.base = BASE_URLS[environment]
        self.session = session or requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
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
        return CAPABILITIES

    def account_snapshot(self) -> AccountSnapshot:
        a = self._call("GET", "/v2/account")
        return AccountSnapshot(cash=_dec(a["cash"]), buying_power=_dec(a["buying_power"]), equity=_dec(a["equity"]),
                               currency=a.get("currency", "USD"), at=datetime.now().astimezone(), raw=a)

    def submit_order(self, req: BrokerOrderRequest) -> BrokerOrder:
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
        self._call("DELETE", f"/v2/orders/{broker_order_id}")

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        o = self._call("GET", f"/v2/orders/{broker_order_id}")
        if o is None:
            raise BrokerError(f"order {broker_order_id} not found at Alpaca")
        return to_order(o)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        o = self._call("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        return to_order(o) if o else None

    def list_fills(self, since: datetime | None) -> list[BrokerFill]:
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

    def positions(self) -> list[BrokerPosition]:
        return [BrokerPosition(symbol=p["symbol"], quantity=_dec(p["qty"]), average_cost=_dec(p["avg_entry_price"]),
                               market_value=_dec(p.get("market_value")))
                for p in self._call("GET", "/v2/positions") or []]
