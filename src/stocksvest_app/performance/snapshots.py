"""Compute and record portfolio and budget snapshots, on a schedule or on request."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..budgets import store as budget
from ..db import iso
from ..deps import AppContext
from ..market.client import MarketError
from ..orders import service as orders_service
from ..portfolios import store as portfolios_store
from ..portfolios.store import D, D0
from . import store

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")  # Used by: scheduled, snapshot_*, routes.budget_performance
INTERVAL = timedelta(minutes=15)  # INTERVAL snapshot spacing. Used by: scheduled
# Prices are ~16 minutes delayed: wait this long after the close before the end-of-day snapshot. Used by: scheduled
AFTER_CLOSE = timedelta(minutes=20)


def _prices(ctx: AppContext, symbols: set[str]) -> dict[str, Decimal]:
    """Latest prices of symbols; {} when the market API is down (positions are then valued at cost).

    Args:
        ctx: app context (market API for prices).
        conn: app DB connection.
        symbols: tickers.

    Used by: portfolio_values, budget_values.
    """
    try:
        snaps = ctx.market.snapshots(symbols)
    except MarketError as e:
        log.warning("snapshot prices unavailable: %s", e)
        return {}
    return {t: Decimal(str(s["latestPrice"])) for t, s in snaps.items() if s.get("latestPrice") is not None}


def _value(lots_positions: list[dict], prices: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """(market value, cost basis). A symbol without a price is valued at its cost.

    Args:
        lots_positions: positions (symbol, quantity, costBasis).
        prices: ticker -> price.

    Used by: portfolio_values, budget_values.
    """
    value = basis = D0
    for x in lots_positions:
        basis += x["costBasis"]
        price = prices.get(x["symbol"])
        value += x["quantity"] * price if price is not None else x["costBasis"]
    return value, basis


def portfolio_values(ctx: AppContext, conn, portfolio: dict, prices: dict | None = None) -> dict:
    """A portfolio's values now.

    Args:
        ctx: app context (market API for prices).
        conn: app DB connection.
        portfolio: the portfolio dict.
        prices: prices to use (default: fetched).

    Returns:
        allocated, cash, unallocated, invested, positions_value, equity, realized_pnl, unrealized_pnl (Decimals).

    Used by: snapshot_portfolio, routes.portfolio_history.
    """
    positions = portfolios_store.positions(conn, portfolio["id"])
    prices = prices if prices is not None else _prices(ctx, {x["symbol"] for x in positions})
    value, basis = _value(positions, prices)
    cash = D(portfolio["cash"])
    return {"allocated": D(portfolio["allocated"]), "cash": cash,
            "unallocated": budget.unallocated_cash(conn, portfolio, orders_service.open_manual_buys(conn, portfolio["id"])),
            "invested": basis, "positions_value": value, "equity": cash + value,
            "realized_pnl": portfolios_store.realized_pnl(conn, portfolio["id"]), "unrealized_pnl": value - basis}


def budget_values(ctx: AppContext, conn, budget_row, date: str, prices: dict | None = None) -> dict:
    """A budget's values now, plus the orders paid from it and their fills on one trading day.

    Args:
        ctx: app context (market API for prices).
        conn: app DB connection.
        budget_row: its budgets row.
        date: New York trading date, YYYY-MM-DD.
        prices: prices to use (default: fetched).

    Returns:
        portfolio_id, allocated, cash, reserved, invested, positions_value, equity, realized_pnl, unrealized_pnl, fees, return_percent, open_positions, orders_placed, fills.

    Used by: snapshot_budgets, routes.budget_performance.
    """
    bid, pid = budget_row["id"], budget_row["portfolio_id"]
    positions = portfolios_store.positions(conn, pid, budget_id=bid)
    prices = prices if prices is not None else _prices(ctx, {x["symbol"] for x in positions})
    value, basis = _value(positions, prices)
    cash, allocated = D(budget_row["cash"]), D(budget_row["allocated"])
    equity = cash + value
    day_start = datetime.fromisoformat(date).replace(tzinfo=ET)
    day = (iso(day_start), iso(day_start + timedelta(days=1)))
    orders = conn.execute("SELECT COUNT(*) FROM orders WHERE budget_id = ? AND created_at >= ? AND created_at < ?",
                          (bid, *day)).fetchone()[0]
    fills = conn.execute("SELECT COUNT(*) FROM fills f JOIN orders o ON o.id = f.order_id WHERE o.budget_id = ? "
                         "AND f.executed_at >= ? AND f.executed_at < ?", (bid, *day)).fetchone()[0]
    return {"portfolio_id": pid, "allocated": allocated, "cash": cash, "reserved": D(budget_row["reserved"]),
            "invested": D(budget_row["invested"]), "positions_value": value, "equity": equity,
            "realized_pnl": D(budget_row["realized_pnl"]), "unrealized_pnl": value - basis,
            "fees": D(budget_row["fees"]),
            "return_percent": ((equity - allocated) / allocated * 100).quantize(Decimal("0.0001")) if allocated else None,
            "open_positions": len(positions), "orders_placed": orders, "fills": fills}


def snapshot_portfolio(ctx: AppContext, conn, portfolio: dict, kind: str, now: datetime) -> dict:
    """Compute and store one portfolio snapshot.

    Args:
        ctx: app context (market API for prices).
        conn: app DB connection.
        portfolio: the portfolio dict.
        kind: INTERVAL, CLOSE or MANUAL.
        now: the snapshot time.

    Returns:
        The stored values.

    Used by: scheduled, routes.snapshot_now.
    """
    values = portfolio_values(ctx, conn, portfolio)
    with conn:
        store.insert_portfolio(conn, portfolio["id"], iso(now), now.astimezone(ET).date().isoformat(), kind, values)
    return values


def snapshot_budgets(ctx: AppContext, conn, now: datetime) -> int:
    """Store today's snapshot of every budget (replacing an earlier one of the day).

    Args:
        ctx: app context (market API for prices).
        conn: app DB connection.
        now: the snapshot time.

    Returns:
        How many were stored.

    Used by: scheduled.
    """
    date = now.astimezone(ET).date().isoformat()
    budgets = conn.execute("SELECT * FROM budgets").fetchall()
    with conn:
        for row in budgets:
            store.insert_budget(conn, row["id"], date, iso(now), budget_values(ctx, conn, row, date))
    return len(budgets)


def scheduled(ctx: AppContext, conn, now: datetime | None = None) -> None:
    """Called every scheduler tick: INTERVAL snapshots each 15 minutes of the regular session, then one CLOSE
    snapshot per portfolio and one per budget after each trading day. Does nothing on closed days or when
    the market API is down.

    Args:
        ctx: app context (market API for prices).
        conn: app DB connection.
        now: the tick time (tests pin it); default now.

    Used by: automations.scheduler.AutomationScheduler._loop.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date().isoformat()
    try:
        day = ctx.market.trading_day(today)
    except MarketError:
        return
    if not day:
        return
    open_, close = datetime.fromisoformat(day["regularOpen"]), datetime.fromisoformat(day["regularClose"])
    portfolios = portfolios_store.list_portfolios(conn)
    if open_ <= now < close:
        slot = open_ + ((now - open_) // INTERVAL) * INTERVAL
        for p in portfolios:
            if not store.has_portfolio_snapshot(conn, p["id"], "INTERVAL", since=iso(slot)):
                snapshot_portfolio(ctx, conn, p, "INTERVAL", now)
    elif now >= close + AFTER_CLOSE:
        taken = False
        for p in portfolios:
            if not store.has_portfolio_snapshot(conn, p["id"], "CLOSE", date=today):
                snapshot_portfolio(ctx, conn, p, "CLOSE", now)
                taken = True
        if taken or not conn.execute("SELECT 1 FROM budget_snapshots WHERE date = ? LIMIT 1", (today,)).fetchone():
            snapshot_budgets(ctx, conn, now)
