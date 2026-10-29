"""The broker interface and the normalized models every adapter returns.

The app never calls a broker API directly: it only uses `Broker`. Each provider adapter maps its own
symbols, order statuses and features onto these models. Money and quantities are Decimal.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

# Normalized order statuses (UML/Orders.mmd). FINAL statuses never change again.
# Used by: adapters (alpaca.STATUS_MAP), orders.service (REJECTED, FINAL_STATUSES), tests.fake_broker.
# orders.store and orders.service also query them by value in SQL.
PENDING = "PENDING"  # created in the app, not yet accepted by the broker
SUBMITTED = "SUBMITTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
FINAL_STATUSES = {FILLED, CANCELLED, REJECTED, EXPIRED}

# Every order type / time in force the app knows; each adapter declares its subset in capabilities().
# Reference lists for adapters (not read at runtime).
ORDER_TYPES = {"MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"}
TIME_IN_FORCE = {"DAY", "GTC", "GTD", "OPG", "CLS", "IOC", "FOK"}


class BrokerError(RuntimeError):
    """A broker call failed.

    Attributes:
        retryable: a temporary problem (network, rate limit, 5xx); otherwise the broker refused the request.

    Used by: every adapter (raised), registry (missing credentials, live/unknown provider), orders.service
    (rejected vs uncertain submit), automations runner/scheduler, and the brokers, orders, portfolios and
    performance routes (-> 4xx/502).
    """

    def __init__(self, message: str, retryable: bool = False):
        """Args:
            message: what went wrong.
            retryable: whether trying again later may work.
        """
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class BrokerCapabilities:
    """What a broker supports; orders are checked against it before anything is sent.

    Attributes:
        order_types: supported ORDER_TYPES.
        time_in_force: supported TIME_IN_FORCE values.
        fractional_shares: fractional quantities allowed.
        extended_hours: extended-hours orders allowed (rules per broker, e.g. Alpaca: LIMIT + DAY only).
        short_selling: selling shares not held allowed (the app never does it).
        max_orders_per_minute: the broker's rate limit.

    Used by: Broker.capabilities (adapters build it), orders.service.check_request, brokers.routes (shown).
    """
    order_types: frozenset[str]
    time_in_force: frozenset[str]
    fractional_shares: bool
    extended_hours: bool  # extended-hours orders allowed (rules per broker, e.g. Alpaca: LIMIT + DAY only)
    short_selling: bool
    max_orders_per_minute: int

    def to_json(self) -> dict:
        """The capabilities in API form (camelCase keys, sorted lists)."""
        return {"orderTypes": sorted(self.order_types), "timeInForce": sorted(self.time_in_force),
                "fractionalShares": self.fractional_shares, "extendedHours": self.extended_hours,
                "shortSelling": self.short_selling, "maxOrdersPerMinute": self.max_orders_per_minute}


@dataclass(frozen=True)
class BrokerOrderRequest:
    """An order to send.

    Attributes:
        client_order_id: the app's id for it (idempotency: resending the same id never doubles the order).
        symbol: ticker.
        side: BUY or SELL.
        type: one of ORDER_TYPES.
        quantity: shares.
        time_in_force: one of TIME_IN_FORCE.
        limit_price: for LIMIT / STOP_LIMIT.
        stop_price: for STOP / STOP_LIMIT.
        trail_percent: for TRAILING_STOP.
        extended_hours: may fill outside regular hours.

    Used by: orders.service.place_order (built), Broker.submit_order.
    """
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
    """An order as the broker reports it.

    Attributes:
        broker_order_id: the broker's id.
        client_order_id: the app's id.
        symbol: ticker.
        side: BUY or SELL.
        type: order type.
        status: normalized status (PENDING ... EXPIRED).
        raw_status: the broker's own value.
        quantity: ordered shares.
        filled_quantity: shares filled so far.
        average_fill_price: average price of the fills, or None.
        reject_reason: why it was rejected, if it was.
        submitted_at: when the broker accepted it.
        updated_at: last change at the broker.

    Used by: Broker.submit_order / get_order / get_order_by_client_id (returned), orders.service.apply_order_update.
    """
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
    """One execution.

    Attributes:
        broker_fill_id: the broker's id (fills are imported once by this id).
        broker_order_id: the order it belongs to.
        symbol: ticker.
        side: BUY or SELL.
        quantity: shares filled.
        price: execution price.
        fee: fees charged.
        executed_at: execution time.

    Used by: Broker.list_fills (returned), orders.service.apply_fill.
    """
    broker_fill_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    executed_at: datetime


@dataclass(frozen=True)
class BrokerFee:
    """A charge on the whole account that is not part of a fill, e.g. Alpaca's daily regulatory (CAT) fee
    "for 8 trades on 2026-10-19".

    Attributes:
        broker_fee_id: the broker's id (imported once by this id).
        amount: what was charged (positive).
        date: the trading day it is for, "YYYY-MM-DD".
        description: the broker's text.

    Used by: Broker.list_fees (returned), orders.service.apply_account_fee.
    """
    broker_fee_id: str
    amount: Decimal
    date: str
    description: str


@dataclass(frozen=True)
class BrokerDividend:
    """Cash a held stock paid (a dividend, positive) or tax taken from it (withholding, negative).

    Attributes:
        broker_dividend_id: the broker's id (imported once by this id).
        symbol: the stock.
        amount: signed cash change: positive = received, negative = withheld.
        date: the day it was paid, "YYYY-MM-DD".
        kind: the broker's type, e.g. Alpaca's DIV or DIVNRA (non-resident tax withheld).
        quantity: shares it was paid on, if the broker says.
        description: the broker's text.

    Used by: Broker.list_dividends (returned), orders.service.apply_dividend.
    """
    broker_dividend_id: str
    symbol: str
    amount: Decimal
    date: str
    kind: str
    quantity: Decimal | None
    description: str


@dataclass(frozen=True)
class BrokerPosition:
    """Shares the broker account holds of one symbol.

    Attributes:
        symbol: ticker.
        quantity: shares.
        average_cost: average entry price.
        market_value: current value, if the broker reports it.

    Used by: Broker.positions (returned), brokers.routes (account positions).
    """
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    market_value: Decimal | None


@dataclass(frozen=True)
class AccountSnapshot:
    """The broker account's balances at one time.

    Attributes:
        cash: cash balance.
        buying_power: what can be spent now.
        equity: cash + positions.
        currency: e.g. USD.
        at: when it was read.
        raw: the broker's full answer (not compared).

    Used by: Broker.account_snapshot (returned), orders.service.sync_account (stored on the account).
    """
    cash: Decimal
    buying_power: Decimal
    equity: Decimal
    currency: str
    at: datetime
    raw: dict = field(default_factory=dict, compare=False)


@runtime_checkable
class Broker(Protocol):
    """What every broker adapter provides. Money and quantities are Decimal.

    Used by: registry.make_broker (returns one), deps.AppContext.broker_factory, orders.service,
    automations.planner/runner, and the brokers, orders, portfolios and performance routes.
    """
    def capabilities(self) -> BrokerCapabilities:
        """What the broker supports."""
        ...

    def account_snapshot(self) -> AccountSnapshot:
        """Current balances.

        Raises:
            BrokerError: the broker can't be reached or refused.
        """
        ...

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        """Send an order.

        Args:
            request: the order.

        Returns:
            The order as accepted (or rejected) by the broker.

        Raises:
            BrokerError: refused (retryable=False) or unknown outcome (retryable=True).
        """
        ...

    def cancel_order(self, broker_order_id: str) -> None:
        """Ask the broker to cancel an open order.

        Args:
            broker_order_id: the broker's id of the order.
        """
        ...

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        """One order's current state.

        Args:
            broker_order_id: the broker's id of the order.
        """
        ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Find an order by the app's id (to resolve submits with an unknown outcome).

        Args:
            client_order_id: the app's id of the order.

        Returns:
            The order, or None if the broker never received it.
        """
        ...

    def list_fills(self, since: datetime | None) -> list[BrokerFill]:
        """Executions, oldest first.

        Args:
            since: only fills after this time (None = all the broker keeps).
        """
        ...

    def list_fees(self, since: datetime | None) -> list[BrokerFee]:
        """Account fees that are not part of a fill (regulatory fees and the like), oldest first.

        Args:
            since: only fees after this time (None = all the broker keeps).
        """
        ...

    def list_dividends(self, since: datetime | None) -> list[BrokerDividend]:
        """Dividends paid and tax withheld from them, oldest first.

        Args:
            since: only ones after this time (None = all the broker keeps).
        """
        ...

    def positions(self) -> list[BrokerPosition]:
        """Shares the account holds now."""
        ...


class SupportsEquityHistory(Protocol):
    """Optional: brokers that can report the account's past equity (checked with hasattr).

    Used by: performance.routes.equity_history (AlpacaBroker implements it).
    """

    def equity_history(self, period: str, timeframe: str) -> list[dict]:
        """The account's past equity.

        Args:
            period: how far back, e.g. "1M".
            timeframe: point spacing, e.g. "1D".
        """
        ...
