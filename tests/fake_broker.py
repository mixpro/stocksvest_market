"""In-memory Broker for tests: same interface as the real adapters; tests fill orders explicitly."""

import itertools
from datetime import datetime, timezone
from decimal import Decimal

from stocksvest_app.brokers.alpaca import CAPABILITIES
from stocksvest_app.brokers.base import (
    CANCELLED,
    FILLED,
    FINAL_STATUSES,
    PARTIALLY_FILLED,
    REJECTED,
    SUBMITTED,
    AccountSnapshot,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
)


class FakeBroker:
    def __init__(self, cash="100000", positions=None):
        self.cash = Decimal(cash)
        self.held: dict[str, tuple[Decimal, Decimal]] = {s: (Decimal(q), Decimal(p)) for s, (q, p) in (positions or {}).items()}
        self.orders: dict[str, BrokerOrder] = {}
        self.fills: list[BrokerFill] = []
        self.ids = itertools.count(1)
        self.fail_next: BrokerError | None = None
        self.submitted: list = []

    def capabilities(self):
        return CAPABILITIES

    def account_snapshot(self):
        return AccountSnapshot(cash=self.cash, buying_power=self.cash * 4, equity=self.cash,
                               currency="USD", at=datetime.now(timezone.utc))

    def submit_order(self, req):
        if self.fail_next:
            err, self.fail_next = self.fail_next, None
            raise err
        self.submitted.append(req)
        now = datetime.now(timezone.utc)
        status = REJECTED if req.symbol == "REJECTME" else SUBMITTED
        o = BrokerOrder(broker_order_id=f"b{next(self.ids)}", client_order_id=req.client_order_id, symbol=req.symbol,
                        side=req.side, type=req.type, status=status, raw_status=status.lower(), quantity=req.quantity,
                        filled_quantity=Decimal(0), average_fill_price=None,
                        reject_reason="test rejection" if status == REJECTED else None, submitted_at=now, updated_at=now)
        self.orders[o.broker_order_id] = o
        return o

    def fill(self, broker_order_id: str, quantity, price, fee="0"):
        """Simulate the exchange filling (part of) an order."""
        o = self.orders[broker_order_id]
        q, p = Decimal(str(quantity)), Decimal(str(price))
        total = o.filled_quantity + q
        o.average_fill_price = ((o.average_fill_price or 0) * o.filled_quantity + p * q) / total
        o.filled_quantity = total
        o.status = FILLED if total == o.quantity else PARTIALLY_FILLED
        o.raw_status = o.status.lower()
        cur_q, cur_p = self.held.get(o.symbol, (Decimal(0), Decimal(0)))
        if o.side == "BUY":
            self.cash -= q * p + Decimal(fee)
            self.held[o.symbol] = (cur_q + q, (cur_q * cur_p + q * p) / (cur_q + q))
        else:
            self.cash += q * p - Decimal(fee)
            self.held[o.symbol] = (cur_q - q, cur_p)
        self.fills.append(BrokerFill(broker_fill_id=f"f{next(self.ids)}", broker_order_id=broker_order_id,
                                     symbol=o.symbol, side=o.side, quantity=q, price=p, fee=Decimal(fee),
                                     executed_at=datetime.now(timezone.utc)))

    def cancel_order(self, broker_order_id):
        o = self.orders[broker_order_id]
        if o.status not in FINAL_STATUSES:
            o.status, o.raw_status = CANCELLED, "canceled"

    def get_order(self, broker_order_id):
        return self.orders[broker_order_id]

    def get_order_by_client_id(self, client_order_id):
        return next((o for o in self.orders.values() if o.client_order_id == client_order_id), None)

    def list_fills(self, since):
        return [f for f in self.fills if since is None or f.executed_at > since]

    def positions(self):
        return [BrokerPosition(symbol=s, quantity=q, average_cost=p, market_value=None)
                for s, (q, p) in self.held.items() if q]

    def equity_history(self, period, timeframe):
        self.history_calls = getattr(self, "history_calls", 0) + 1
        return [{"at": "2026-10-15T20:00:00Z", "equity": 99500.0, "profitLoss": -500.0},
                {"at": "2026-10-16T20:00:00Z", "equity": 100000.0, "profitLoss": 500.0}]
