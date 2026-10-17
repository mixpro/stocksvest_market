"""The broker interface and the normalized models every adapter returns.

The app never calls a broker API directly: it only uses `Broker`. Each provider adapter maps its own
symbols, order statuses and features onto these models. Money and quantities are Decimal.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

# Normalized order statuses (UML/Orders.mmd). FINAL statuses never change again.
PENDING = "PENDING"  # created in the app, not yet accepted by the broker
SUBMITTED = "SUBMITTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
FINAL_STATUSES = {FILLED, CANCELLED, REJECTED, EXPIRED}

ORDER_TYPES = {"MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"}
TIME_IN_FORCE = {"DAY", "GTC", "GTD", "OPG", "CLS", "IOC", "FOK"}


class BrokerError(RuntimeError):
    """`retryable` = a temporary problem (network, rate limit); otherwise the broker refused the request."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class BrokerCapabilities:
    order_types: frozenset[str]
    time_in_force: frozenset[str]
    fractional_shares: bool
    extended_hours: bool  # extended-hours orders allowed (rules per broker, e.g. Alpaca: LIMIT + DAY only)
    short_selling: bool
    max_orders_per_minute: int

    def to_json(self) -> dict:
        return {"orderTypes": sorted(self.order_types), "timeInForce": sorted(self.time_in_force),
                "fractionalShares": self.fractional_shares, "extendedHours": self.extended_hours,
                "shortSelling": self.short_selling, "maxOrdersPerMinute": self.max_orders_per_minute}


@dataclass(frozen=True)
class BrokerOrderRequest:
    client_order_id: str
    symbol: str
    side: str  # BUY | SELL
    type: str
    quantity: Decimal
    time_in_force: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_percent: Decimal | None = None
    extended_hours: bool = False


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    status: str  # normalized
    raw_status: str  # the broker's own value
    quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    reject_reason: str | None
    submitted_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class BrokerFill:
    broker_fill_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    executed_at: datetime


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    market_value: Decimal | None


@dataclass(frozen=True)
class AccountSnapshot:
    cash: Decimal
    buying_power: Decimal
    equity: Decimal
    currency: str
    at: datetime
    raw: dict = field(default_factory=dict, compare=False)


@runtime_checkable
class Broker(Protocol):
    def capabilities(self) -> BrokerCapabilities: ...

    def account_snapshot(self) -> AccountSnapshot: ...

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def get_order(self, broker_order_id: str) -> BrokerOrder: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def list_fills(self, since: datetime | None) -> list[BrokerFill]: ...

    def positions(self) -> list[BrokerPosition]: ...
