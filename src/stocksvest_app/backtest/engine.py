"""Replay a Buy and a Sell automation over history with the app's own filter engine and order planner.

Each automation runs in every clock hour its schedule fires in (steps SCHEDULE, the default), once, at the start of
that hour: bars are hourly, so the run sees the prices as of the hour and fills inside it (a 09:30 fire runs at
09:00). Steps HOURLY instead runs both every trading hour from 10:00 to 15:00 New York (the old way, kept for
comparison). At the same time the Sell automation runs first, then the Buy automation, exactly as the live runner
does them: evaluate the run targets, planner.select, put live quotes (modelled spreads) into the plan, planner.size,
then fill each PLACE intent. Fill modes: NEXT_VWAP (default) fills at the next hour's VWAP at the modelled ask/bid,
so a signal never trades at the very print that triggered it (NEXT_VWAP_STRICT: before the hourly history, the next
day's VWAP instead of the same day's); SAME_CLOSE fills at once at the planner's estimated
price (the old, optimistic way, kept for comparison). Shares are kept as position lots in a scratch copy of the app
database, so filters that read the portfolio (INVESTMENT_PORTFOLIO inputs, POSITION, UNREALIZED_PNL) see the
simulated holdings.

Simplifications, listed in every report: at most one run an hour instead of the live 1- and 15-minute schedules;
market orders fill completely at the modelled bid/ask (a buy shrinks to the cash there is); no fees (Alpaca paper);
today's halal universe throughout.

Used by: backtest.__main__, tests.
"""

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import TypeAdapter

from .. import db
from ..automations import planner, timing
from ..automations import store as automations_store
from ..automations.models import AutomationIn, OrderGuards, Schedule
from ..brokers.base import BrokerCapabilities
from ..budgets import store as budget_store
from ..filters import store as filters_store
from ..filters.engine import Evaluator
from ..filters.models import FilterIn
from ..market.data import MarketData
from ..portfolios import store as portfolios_store
from .history import ET, HistoricalMarket

STEP_HOURS = range(10, 16)  # 10:00 .. 15:00 New York. Used by: run
FILL_MODES = ("NEXT_VWAP", "NEXT_VWAP_STRICT", "SAME_CLOSE")  # see the module docstring. Used by: run, __main__
STEP_MODES = ("SCHEDULE", "HOURLY")  # see the module docstring. Used by: run, __main__
SCHEDULES = TypeAdapter(list[Schedule])  # Used by: _fire_times
DELISTED_AFTER_DAYS = 5  # a held stock without a daily bar for this many trading days is closed. Used by: run
LIQUID_DOLLARS = 50e6  # the liquid benchmark's minimum 20-day average dollar volume. Used by: run


class _Broker:
    """Capabilities only (the planner's order checks); nothing is ever sent. Used by: run."""

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(order_types={"MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"},
                                  time_in_force={"DAY", "GTC", "OPG", "CLS", "IOC", "FOK"}, fractional_shares=False,
                                  extended_hours=True, short_selling=False, max_orders_per_minute=200)


@dataclass
class Book:
    """The simulated budget: cash, trades, the cooldown's last sells and the equity curve."""

    cash: Decimal
    trades: list[dict] = field(default_factory=list)
    last_sells: dict[str, datetime] = field(default_factory=dict)
    equity: list[dict] = field(default_factory=list)


def _targets(conn, ev: Evaluator, automation: dict) -> tuple[set[str], dict[str, str]]:
    """The run targets' union, as runner._run_targets computes it, plus which filter matched each symbol first.

    Args:
        conn: app DB connection.
        ev: the step's Evaluator.
        automation: the automation dict.

    Returns:
        (matched tickers, ticker -> name of the first filter that matched it).
    """
    union, first = set(), {}
    for target in automation["runTargets"]:
        ids = [target["id"]] if target["type"] == "FILTER" else filters_store.get_group(conn, target["id"])["filterIds"]
        for fid in ids:
            f = filters_store.get_filter(conn, fid)
            out = ev.evaluate(FilterIn.model_validate(f), (fid,))["symbols"]
            for t in out:
                first.setdefault(t, f["name"])
            union |= out
    return union, first


def _context(conn, hist: HistoricalMarket, ev: Evaluator, book: Book, portfolio_id: str, budget_id: str,
             matched: set[str], now: datetime) -> planner.PlanContext:
    """The budget's PlanContext at `now`, built like runner.Trading.context."""
    holdings = {p["symbol"]: planner.Holding(p["quantity"], p["costBasis"])
                for p in portfolios_store.positions(conn, portfolio_id, budget_id=budget_id)}
    snaps = ev._snaps(set(matched) | set(holdings))
    invested = sum((h.quantity * Decimal(str(snaps.get(t, {}).get("latestPrice") or 0)) for t, h in holdings.items()),
                   Decimal(0))
    return planner.PlanContext(snapshots=dict(snaps), holdings=holdings, open_buys=set(), open_sells={},
                               budget_available=book.cash, budget_equity=book.cash + invested, session="REGULAR",
                               data_age_minutes=0, last_sells=dict(book.last_sells), now=now,
                               sectors={t: s.get("sector") for t, s in ev.market.universe().items()})


def _fire_times(automation: dict, day: date, calendar: timing.CalendarFn, created: datetime) -> list[datetime]:
    """The starts of the clock hours (New York) the automation fires in on `day`.

    Args:
        automation: the automation dict.
        day: the trading day.
        calendar: trading days for MARKET schedules.
        created: anchor for WEEKLY / INTERVAL schedules without startAt (the backtest's start).
    """
    lo = datetime.combine(day, time(), ET)
    hours: set[datetime] = set()
    for s in SCHEDULES.validate_python(automation["schedules"]):
        for t in timing.fires_between(s, lo - timedelta(seconds=1), lo + timedelta(days=1, seconds=-1), created,
                                      calendar, cap=2000):
            hours.add(t.astimezone(ET).replace(minute=0, second=0, microsecond=0))
    return sorted(hours)


def _run_automation(conn, hist, market, book, automation, portfolio_id, budget_id, now, broker, fill) -> None:
    """One run of an automation at `now`: evaluate, plan each PLACE_ORDER action, fill its PLACE intents."""
    ev = Evaluator(conn, market, now=now)
    matched, first = _targets(conn, ev, automation)
    guards = OrderGuards.model_validate(automation.get("orderGuards") or {})
    ctx = None
    for action in AutomationIn.model_validate(automation).actions:
        if action.type != "PLACE_ORDER":
            continue
        ctx = ctx or _context(conn, hist, ev, book, portfolio_id, budget_id, matched, now)
        planner.add_rank_data(action, sorted(matched), ctx.snapshots, market, guards, held=ctx.holdings)
        chosen = planner.select(action, sorted(matched), ctx, guards)
        if not planner.market_blocked(guards, ctx):
            quotes = hist.quotes(chosen["symbols"])["quotes"]
            for t, q in quotes.items():
                ctx.snapshots[t] = {**ctx.snapshots.get(t, {}), "latestPrice": q["price"], "bid": q["bid"],
                                    "ask": q["ask"], "priceSource": "IBKR", "quoteAgeSeconds": 0}
        for intent in planner.size(action, guards, chosen["symbols"], ctx, broker):
            if intent["decision"] != "PLACE":
                continue
            price = Decimal(str(intent["estimatedPrice"]))
            if fill in ("NEXT_VWAP", "NEXT_VWAP_STRICT"):
                nxt = hist.next_fill(intent["ticker"], intent["side"], strict=fill == "NEXT_VWAP_STRICT")
                if nxt is None:
                    continue  # no trade left today: the order would expire unfilled
                price = Decimal(str(nxt))
            _fill(conn, book, automation, portfolio_id, budget_id, intent, first.get(intent["ticker"]), now, price)


def _fill(conn, book: Book, automation: dict, portfolio_id: str, budget_id: str, intent: dict, reason: str | None,
          now: datetime, price: Decimal) -> None:
    """Fill a PLACE intent completely at `price` and book it (a buy shrinks to whole shares the cash pays for)."""
    t, qty = intent["ticker"], Decimal(str(intent["quantity"]))
    at = now.astimezone(timezone.utc).isoformat()
    if intent["side"] == "BUY":
        qty = min(qty, (book.cash / price).to_integral_value(rounding="ROUND_FLOOR"))
        if qty <= 0:
            return
    with conn:
        if intent["side"] == "BUY":
            portfolios_store.open_lot(conn, portfolio_id, t, automation["id"], qty, price, opened_at=at,
                                      budget_id=budget_id)
            book.cash -= qty * price
            book.trades.append({"time": at, "side": "BUY", "symbol": t, "quantity": float(qty), "price": float(price),
                                "automation": automation["name"], "filter": reason})
        else:
            lots = portfolios_store.open_lots(conn, portfolio_id, t, budget_id=budget_id)
            opened = min(lot["opened_at"] for lot in lots) if lots else at
            taken, cost, pnl = portfolios_store.close_lots_fifo(conn, portfolio_id, t, budget_id, qty, price)
            book.cash += taken * price
            book.last_sells[t] = now
            book.trades.append({"time": at, "side": "SELL", "symbol": t, "quantity": float(taken), "price": float(price),
                                "automation": automation["name"], "filter": reason, "pnl": float(pnl),
                                "pnlPct": float(pnl / cost * 100) if cost else None,
                                "heldHours": round((now - datetime.fromisoformat(opened)).total_seconds() / 3600, 1)})


def _close_delisted(conn, hist: HistoricalMarket, book: Book, automation: dict, portfolio_id: str, budget_id: str,
                    days: list[str], day: str, now: datetime) -> None:
    """Sell, at its last close, every held stock whose bars ended DELISTED_AFTER_DAYS trading days ago (delisted:
    bought out, merged or gone). ponytail: the last close is kind to bankruptcies, which often end lower."""
    i = days.index(day)
    if i < DELISTED_AFTER_DAYS:
        return
    for p in portfolios_store.positions(conn, portfolio_id, budget_id=budget_id):
        last = hist.last_bar(p["symbol"])
        if last and last[0] < days[i - DELISTED_AFTER_DAYS]:
            intent = {"ticker": p["symbol"], "side": "SELL", "quantity": p["quantity"]}
            _fill(conn, book, automation, portfolio_id, budget_id, intent, "DELISTED", now, Decimal(str(last[1])))


def _equity(conn, hist: HistoricalMarket, book: Book, portfolio_id: str, budget_id: str) -> tuple[float, float]:
    """(equity, invested) at hist.now: cash plus every held share at the modelled bid."""
    held = portfolios_store.positions(conn, portfolio_id, budget_id=budget_id)
    quotes = hist.quotes([p["symbol"] for p in held])["quotes"]
    value = sum((p["quantity"] * Decimal(str(quotes[p["symbol"]]["bid"])) for p in held if p["symbol"] in quotes),
                Decimal(0))
    return float(book.cash + value), float(value)


def prepare(app_db: Path, work_dir: Path) -> Path:
    """A scratch copy of the app database with every simulated table emptied (definitions stay, ids unchanged).

    Args:
        app_db: a COPY of app.db (e.g. the nightly backup).
        work_dir: where to put the scratch copy.
    """
    work = work_dir / "backtest-app.db"
    src, conn = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True), sqlite3.connect(work)
    src.backup(conn)  # SQLite's own copy: includes anything still in the source's write-ahead log
    src.close()
    conn.execute("DELETE FROM position_lots")
    conn.commit()
    conn.close()
    return work


def run(app_db: Path, market_db: Path, buy_id: str, sell_id: str, start: date, end: date, budget: float = 10_000,
        overrides: dict | None = None, hist: HistoricalMarket | None = None, progress=None,
        fill: str = "NEXT_VWAP", steps: str = "SCHEDULE") -> dict:
    """Backtest a Buy and a Sell automation from `start` to `end` (inclusive) on one budget.

    Args:
        app_db: a COPY of app.db holding the automations, their filters and groups, and the budget.
        market_db: a COPY of market.db.
        buy_id: the Buy automation's id.
        sell_id: the Sell automation's id.
        start: first trading day.
        end: last trading day.
        budget: starting cash.
        overrides: {"buy": {...}, "sell": {...}, "filters": {filter_id: {...}}} fields merged into the stored
            definitions before the run (e.g. {"buy": {"orderGuards": {"maxSpreadPercent": 0.5}}}).
        hist: an already loaded HistoricalMarket (reused across runs); default loads one.
        progress: called with each finished day ("YYYY-MM-DD", equity).
        fill: NEXT_VWAP or SAME_CLOSE (see the module docstring).
        steps: SCHEDULE or HOURLY (see the module docstring).

    Returns:
        {"params", "summary", "equity" (per day), "trades"}.
    """
    if fill not in FILL_MODES:
        raise ValueError(f"fill must be one of {FILL_MODES}")
    if steps not in STEP_MODES:
        raise ValueError(f"steps must be one of {STEP_MODES}")
    overrides = overrides or {}
    hist = hist or HistoricalMarket(market_db, start, end)
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(prepare(app_db, Path(tmp)))
        try:
            for fid, patch in overrides.get("filters", {}).items():
                f = {**filters_store.get_filter(conn, fid), **patch}
                conn.execute("UPDATE filters SET inputs = ?, actions = ? WHERE id = ?",
                             (json.dumps(f["inputs"]), json.dumps(f["actions"]), fid))
            conn.commit()
            autos = {}
            for key, aid in (("buy", buy_id), ("sell", sell_id)):
                a = automations_store.get_automation(conn, aid)
                patch = overrides.get(key, {})
                if "orderGuards" in patch:
                    patch = {**patch, "orderGuards": {**(a.get("orderGuards") or {}), **patch["orderGuards"]}}
                autos[key] = {**a, **patch}
            budget_ref = budget_store.budgets_of(conn, buy_id)[0]
            portfolio_id, budget_id = budget_ref["portfolioId"], budget_ref["id"]
            book = Book(cash=Decimal(str(budget)))
            market = MarketData(hist)
            broker = _Broker()
            days = [r["date"] for r in hist.calendar_rows if start.isoformat() <= r["date"] <= end.isoformat()]
            calendar = lambda a, b: hist.calendar(a.isoformat(), b.isoformat())["days"]  # noqa: E731
            created = datetime.combine(start, time(), ET)
            for day in days:
                d = date.fromisoformat(day)
                close = datetime.fromisoformat(next(r["close"] for r in hist.calendar_rows if r["date"] == day))
                if steps == "HOURLY":
                    runs = [(datetime.combine(d, time(h), ET), i) for h in STEP_HOURS for i in (0, 1)
                            if datetime.combine(d, time(h), ET) < close]
                else:
                    runs = sorted((t, i) for i, key in enumerate(("sell", "buy"))
                                  for t in _fire_times(autos[key], d, calendar, created))
                for now, i in runs:  # i: 0 = Sell, 1 = Buy, so at the same time the Sell runs first
                    hist.now = now
                    _run_automation(conn, hist, market, book, autos[("sell", "buy")[i]], portfolio_id, budget_id,
                                    now, broker, fill)
                hist.now = close  # ponytail: equity at the regular close, after-hours fires land in the next day
                _close_delisted(conn, hist, book, autos["sell"], portfolio_id, budget_id, days, day, close)
                equity, invested = _equity(conn, hist, book, portfolio_id, budget_id)
                book.equity.append({"date": day, "equity": round(equity, 2), "invested": round(invested, 2),
                                    "cash": round(float(book.cash), 2)})
                if progress:
                    progress(day, equity)
        finally:
            conn.close()
    summary = summarize(book, budget)
    summary["buyHold"] = {"allPct": buy_and_hold(hist, days), "liquidPct": buy_and_hold(hist, days, LIQUID_DOLLARS)}
    return {"params": {"start": start.isoformat(), "end": end.isoformat(), "budget": budget, "fill": fill,
                       "steps": steps,
                       "buy": autos["buy"],
                       "sell": autos["sell"], "overrides": overrides},
            "summary": summary, "equity": book.equity, "trades": book.trades}


def buy_and_hold(hist: HistoricalMarket, days: list[str], min_dollars: float = 0.0) -> float | None:
    """Benchmark return in percent: equal dollars in every symbol with a close on the first day (and, with
    min_dollars, a 20-day average dollar volume above it before the first day), bought and held, no costs. A symbol
    whose bars end before the last day (delisted) counts at its last close.

    Args:
        hist: the loaded market.
        days: the backtest's trading days.
        min_dollars: liquidity floor; 0 = the whole universe.
    """
    if not days:
        return None
    hist.now = datetime.combine(date.fromisoformat(days[0]), time(9), ET)  # volume of the days before the start
    ratios = []
    for t in hist.by_ticker:
        a, b = hist._daily_on(t, days[0]), hist._daily_on(t, days[-1])
        last = hist.last_bar(t)
        if a and not b and last and days[0] < last[0] < days[-1]:
            b = last[1]
        if a and b and (not min_dollars or (hist.avg_dollar_volume(t) or 0) > min_dollars):
            ratios.append(b / a)
    return round((sum(ratios) / len(ratios) - 1) * 100, 2) if ratios else None


def summarize(book: Book, budget: float) -> dict:
    """Headline figures: return, drawdown, daily results, round trips and what each sell filter did.

    Args:
        book: the finished Book.
        budget: starting cash.
    """
    eq = [budget] + [e["equity"] for e in book.equity]
    daily = [b - a for a, b in zip(eq, eq[1:])]
    peak, drawdown = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        drawdown = min(drawdown, v - peak)
    sells = [t for t in book.trades if t["side"] == "SELL"]
    by_filter: dict[str, dict] = {}
    for s in sells:
        f = by_filter.setdefault(s["filter"] or "?", {"sells": 0, "pnl": 0.0, "wins": 0})
        f["sells"] += 1
        f["pnl"] = round(f["pnl"] + s["pnl"], 2)
        f["wins"] += s["pnl"] > 0
    wins = [s["pnl"] for s in sells if s["pnl"] > 0]
    losses = [s["pnl"] for s in sells if s["pnl"] <= 0]
    return {
        "finalEquity": round(eq[-1], 2), "returnUsd": round(eq[-1] - budget, 2),
        "returnPct": round((eq[-1] / budget - 1) * 100, 2), "maxDrawdownUsd": round(drawdown, 2),
        "tradingDays": len(daily), "positiveDays": sum(1 for d in daily if d > 0),
        "bestDayUsd": round(max(daily), 2) if daily else None, "worstDayUsd": round(min(daily), 2) if daily else None,
        "buys": sum(1 for t in book.trades if t["side"] == "BUY"), "sells": len(sells),
        "winRate": round(len(wins) / len(sells), 3) if sells else None,
        "avgWinUsd": round(sum(wins) / len(wins), 2) if wins else None,
        "avgLossUsd": round(sum(losses) / len(losses), 2) if losses else None,
        "avgHeldHours": round(sum(s["heldHours"] for s in sells) / len(sells), 1) if sells else None,
        "bySellFilter": by_filter,
    }
