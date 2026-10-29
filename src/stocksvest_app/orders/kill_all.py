"""The emergency stop: switch all scheduled automation runs off, cancel open buys, and sell every share the app's
portfolios hold (paper and live) at market. Shares in the broker account that belong to no portfolio are left alone.
"""

import logging

from ..audit import store as audit
from ..automations import store as automations_store
from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..portfolios import store as pf
from ..portfolios.store import D0
from . import service, store

log = logging.getLogger(__name__)


def kill_all(ctx, conn, reason: str = "kill all") -> dict:
    """Stop everything and sell everything. Each step goes on when one fails; failures are listed.

    Args:
        ctx: app context (broker factory).
        conn: app DB connection.
        reason: shown on the automation switch.

    Returns:
        {"cancelled": [order ids], "sells": [orders], "errors": [text]}.

    Used by: orders.routes.kill_all.
    """
    automations_store.set_switch(conn, False, reason)
    out = {"cancelled": [], "sells": [], "errors": []}
    for p in pf.list_portfolios(conn):
        try:
            broker = ctx.broker_factory(brokers_store.get_account(conn, p["brokerAccountId"]))
        except BrokerError as e:
            out["errors"].append(f"{p['name']}: {e}")
            continue
        for o in store.list_orders(conn, portfolio_id=p["id"], open_only=True, limit=1000):
            if o["side"] == "BUY":
                try:
                    service.cancel_order(conn, broker, o["id"])
                    out["cancelled"].append(o["id"])
                except (BrokerError, service.OrderRejected) as e:
                    out["errors"].append(f"{p['name']}: cancel {o['symbol']} buy: {e}")
        owned: dict = {}
        for row in pf.positions(conn, p["id"], by_owner=True):
            owned[(row["symbol"], row["budgetId"])] = owned.get((row["symbol"], row["budgetId"]), D0) + row["quantity"]
        for (symbol, budget_id), qty in sorted(owned.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            free = qty - service.open_sell_quantity(conn, p["id"], symbol, budget_id)
            if free <= 0:
                continue  # already being sold
            try:
                out["sells"].append(service.place_order(conn, broker, p, symbol=symbol, side="SELL", type="MARKET",
                                                        quantity=free, time_in_force="DAY", budget_id=budget_id))
            except (BrokerError, service.OrderRejected) as e:
                out["errors"].append(f"{p['name']}: sell {free} {symbol}: {e}")
    audit.record(conn, "KILL_ALL", outcome="FAILED" if out["errors"] else "OK",
                 details={"reason": reason, "cancelled": len(out["cancelled"]), "sells": len(out["sells"]),
                          "errors": out["errors"]})
    log.warning("kill all: %d buys cancelled, %d sells placed, %d errors", len(out["cancelled"]), len(out["sells"]),
                len(out["errors"]))
    return out
