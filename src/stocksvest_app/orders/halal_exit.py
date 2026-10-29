"""Shares of stocks that left the halal universe. The market service lists removed symbols with the reason; held
NON_COMPLIANT ones are sold at market during the next regular session, each owner (budget or manual shares) selling
its own shares. Other reasons (PENDING, ERROR, NOT_IN_HALAL_DB, NOT_TRADABLE) are only reported: a screening glitch
must not force a sale, and a stock that is no longer tradable can't be sold by an order.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..market.client import MarketError
from ..portfolios import store as pf
from ..portfolios.store import D0
from . import service

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SELL_REASONS = {"NON_COMPLIANT"}  # Used by: run


def held_removed(ctx, conn) -> list[dict]:
    """Holdings of removed symbols, per portfolio and owner.

    Args:
        ctx: app context (market API).
        conn: app DB connection.

    Returns:
        [{portfolio, budgetId, symbol, quantity, reason, removedAt}] (portfolio is the dict).

    Raises:
        MarketError: the market service failed.

    Used by: run, alerts.checks.check (daily email).
    """
    removed = {r["ticker"]: r for r in ctx.market_api.removed_symbols()["symbols"]}
    out = []
    for p in pf.list_portfolios(conn) if removed else []:
        owned: dict = {}
        for row in pf.positions(conn, p["id"], by_owner=True):
            if row["symbol"] in removed:
                key = (row["symbol"], row["budgetId"])
                owned[key] = owned.get(key, D0) + row["quantity"]
        for (symbol, budget_id), qty in sorted(owned.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            out.append({"portfolio": p, "budgetId": budget_id, "symbol": symbol, "quantity": qty,
                        "reason": removed[symbol]["reason"], "removedAt": removed[symbol]["removedAt"]})
    return out


def run(ctx, conn, now: datetime | None = None) -> list[dict]:
    """During the regular session, sell what is held of NON_COMPLIANT symbols (MARKET, DAY), skipping shares an open
    sell order already covers. Failures are logged; the next pass retries.

    Args:
        ctx: app context (market API, broker factory).
        conn: app DB connection.
        now: pass time (tests pin it); default now.

    Returns:
        The orders placed.

    Used by: automations.scheduler.AutomationScheduler._loop (after each broker sync).
    """
    now = now or datetime.now(timezone.utc)
    try:
        held = [h for h in held_removed(ctx, conn) if h["reason"] in SELL_REASONS]
        day = ctx.market.trading_day(now.astimezone(ET).date().isoformat()) if held else None
    except MarketError as e:
        log.warning("halal exit check failed: %s", e.detail)
        return []
    if not day or not (datetime.fromisoformat(day["regularOpen"]) <= now < datetime.fromisoformat(day["regularClose"])):
        return []
    placed = []
    for h in held:
        p, symbol, budget_id = h["portfolio"], h["symbol"], h["budgetId"]
        free = h["quantity"] - service.open_sell_quantity(conn, p["id"], symbol, budget_id)
        if free <= 0:
            continue
        try:
            broker = ctx.broker_factory(brokers_store.get_account(conn, p["brokerAccountId"]))
            order = service.place_order(conn, broker, p, symbol=symbol, side="SELL", type="MARKET", quantity=free,
                                        time_in_force="DAY", budget_id=budget_id)
        except (BrokerError, service.OrderRejected) as e:
            log.warning("halal exit sell of %s %s in %s failed: %s", free, symbol, p["name"], e)
            continue
        log.info("halal exit: sell %s %s in %s (%s)", free, symbol, p["name"], order["status"])
        placed.append(order)
    return placed
