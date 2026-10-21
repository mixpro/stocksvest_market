"""Run one automation: checks, market context, filter targets, actions, and the run record."""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

from decimal import Decimal

from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..db import new_id
from ..deps import AppContext
from ..orders import service as orders_service
from ..orders import store as orders_store
from ..portfolios import store as portfolios_store
from . import planner
from ..budgets import store as budget_store
from .models import OrderGuards, PlaceOrderAction
from ..filters import store as filters_store
from ..filters.engine import EngineError, Evaluator
from ..filters.models import FilterIn
from ..market.client import MarketError
from ..runs import store as runs_store
from . import store
from .timing import ET

log = logging.getLogger(__name__)

# One run per automation at a time, and one order-placing run per portfolio at a time (a run takes the locks of
# all its budgets' portfolios, in sorted order).
# Used by: _lock_for, run_automation (tests hold a lock to simulate an overlapping run).
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    """The lock for an automation id or "portfolio:<id>", created on first use.

    Args:
        key: the lock's name.
    """
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class Trading:
    """Portfolio, broker and planning state of one budget during a run of an automation that places orders.

    Attributes:
        budget_id: the budget the orders are paid from / sell for.
        name: the budget's name.
        portfolio: the budget's portfolio (refreshed after the broker sync).
        account: its broker account.
        broker: the Broker adapter (ctx.broker_factory).
        guards: the automation's OrderGuards (defaults when missing), applied per budget.
        plan_ctx: PlanContext, built on first use and shared by every order action of the run.

    Used by: run_automation, _place_orders.
    """

    def __init__(self, ctx: AppContext, conn, automation: dict, budget: dict, preview: bool, synced: set[str]):
        """Load the portfolio and broker; for real runs, sync the broker account first (once per run) so fills and
        balances are current.

        Args:
            ctx: app context.
            conn: app DB connection.
            automation: the automation dict.
            budget: the budget dict (budgets.store.get_budget).
            preview: skip the broker sync (previews change nothing).
            synced: broker account ids already synced in this run (updated).

        Raises:
            ValueError: the portfolio no longer exists.
            BrokerError: the broker can't be reached or synced.
        """
        self.conn, self.automation = conn, automation
        self.budget_id, self.name = budget["id"], budget["name"]
        self.portfolio = portfolios_store.get_portfolio(conn, budget["portfolioId"])
        if not self.portfolio:
            raise ValueError("the budget's portfolio no longer exists")
        self.account = brokers_store.get_account(conn, self.portfolio["brokerAccountId"])
        self.broker = ctx.broker_factory(self.account)
        if not preview and self.account["id"] not in synced:  # current fills and balances before deciding anything
            orders_service.sync_account(conn, self.broker, self.account)
            synced.add(self.account["id"])
            self.portfolio = portfolios_store.get_portfolio(conn, self.portfolio["id"])
        self.guards = OrderGuards.model_validate(automation.get("orderGuards") or {})
        self.plan_ctx: planner.PlanContext | None = None

    def budget(self) -> dict | None:
        """The budget now, or None if it was deleted meanwhile."""
        return budget_store.get_budget(self.conn, self.budget_id)

    def budget_snapshot(self) -> dict | None:
        """Budget figures for the run record (before / after), or None if the budget is gone."""
        b = self.budget()
        if not b:
            return None
        return {k: b[k] for k in ("cash", "reserved", "available", "invested", "realizedPnL", "fees", "allocated")}

    def context(self, snapshots: dict, session, data_age) -> planner.PlanContext:
        """The budget's PlanContext, built once from the budget's shares, its open orders (from any of its
        automations) and its money.

        Args:
            snapshots: market snapshots of the matched symbols.
            session: market session now.
            data_age: market data age in minutes.
        """
        if self.plan_ctx is None:
            bid, pid = self.budget_id, self.portfolio["id"]
            holdings = {p["symbol"]: planner.Holding(p["quantity"], p["costBasis"])
                        for p in portfolios_store.positions(self.conn, pid, budget_id=bid)}
            open_orders = orders_store.list_orders(self.conn, pid, budget_id=bid, open_only=True, limit=1000)
            open_sells: dict[str, Decimal] = {}
            for o in open_orders:
                if o["side"] == "SELL":
                    open_sells[o["symbol"]] = open_sells.get(o["symbol"], Decimal(0)) + Decimal(
                        str(o["quantity"] - o["filledQuantity"]))
            b = self.budget()
            self.plan_ctx = planner.PlanContext(
                snapshots=snapshots, holdings=holdings,
                open_buys={o["symbol"] for o in open_orders if o["side"] == "BUY"}, open_sells=open_sells,
                budget_available=Decimal(str(b["available"])) if b else Decimal(0),
                budget_equity=Decimal(str(b["cash"] + b["invested"])) if b else Decimal(0),
                session=session, data_age_minutes=data_age)
        return self.plan_ctx


def market_context(ctx: AppContext, now: datetime) -> dict:
    """Which session the market is in and how old the synced data is.

    Args:
        ctx: app context (market API and helpers).
        now: the run time.

    Returns:
        {"session", "marketLastSyncAt", "dataAgeMinutes"} (+ "error" when the market API failed).

    Used by: run_automation (tests replace it to pin the session and data age).
    """
    out = {"session": None, "marketLastSyncAt": None, "dataAgeMinutes": None}
    try:
        status = ctx.market_api.status()
        last = status.get("lastIncrementalAt")
        out["marketLastSyncAt"] = last
        if last:
            out["dataAgeMinutes"] = int((now - datetime.fromisoformat(last)).total_seconds() // 60)
        day = ctx.market.trading_day(now.astimezone(ET).date().isoformat())
    except MarketError as e:
        out["error"] = e.detail
        return out
    if not day:
        out["session"] = "CLOSED"
    else:
        t = lambda k: datetime.fromisoformat(day[k])  # noqa: E731
        if t("preMarketOpen") <= now < t("regularOpen"):
            out["session"] = "PRE_MARKET"
        elif t("regularOpen") <= now < t("regularClose"):
            out["session"] = "REGULAR"
        elif t("regularClose") <= now < t("afterHoursClose"):
            out["session"] = "AFTER_HOURS"
        else:
            out["session"] = "CLOSED"
    return out


def _render(text: str, automation: dict, symbols: list[str]) -> str:
    """Fill {name} and {count} in a notification title or message.

    Args:
        text: the template.
        automation: provides the name.
        symbols: matched tickers (count).
    """
    return text.replace("{name}", automation["name"]).replace("{count}", str(len(symbols)))


def _run_targets(conn, ctx: AppContext, automation: dict) -> tuple[list[dict], set[str], Evaluator]:
    """Run the automation's filters and groups.

    Args:
        conn: app DB connection.
        ctx: app context.
        automation: the automation dict.

    Returns:
        (per-target results with steps and counts, union of matched tickers, the Evaluator used).

    Raises:
        EngineError: a target no longer exists, or a filter can't run.
        MarketError: market data unavailable.
    """
    ev = Evaluator(conn, ctx.market)
    targets, union = [], set()
    for target in automation["runTargets"]:
        if target["type"] == "FILTER":
            f = filters_store.get_filter(conn, target["id"])
            if not f:
                raise EngineError(f"filter '{target['id']}' no longer exists")
            out = ev.evaluate(FilterIn.model_validate(f), (f["id"],))
            targets.append({"targetId": f["id"], "type": "FILTER", "name": f["name"], "steps": out["steps"],
                            "count": len(out["symbols"])})
            union |= out["symbols"]
        else:
            g = filters_store.get_group(conn, target["id"])
            if not g:
                raise EngineError(f"filter group '{target['id']}' no longer exists")
            members, group_union = [], set()
            for fid in g["filterIds"]:
                f = filters_store.get_filter(conn, fid)
                out = ev.evaluate(FilterIn.model_validate(f), (fid,))
                members.append({"filterId": fid, "name": f["name"], "steps": out["steps"], "count": len(out["symbols"])})
                group_union |= out["symbols"]
            targets.append({"targetId": g["id"], "type": "FILTER_GROUP", "name": g["name"], "filters": members,
                            "count": len(group_union)})
            union |= group_union
    return targets, union, ev


def run_automation(ctx: AppContext, conn: sqlite3.Connection, automation: dict, trigger: str,
                   scheduled_for: datetime | None = None, schedule_id: str | None = None,
                   fire_id: str | None = None, preview: bool = False) -> dict:
    """Execute (or preview) one automation. A real run is stored in the run history and returned;
    a preview returns the same shape without storing anything or creating notifications.

    Filters run once; each order action then plans and places orders separately for every budget the automation is
    in (result["budgets"] has each budget's money before and after). A budget whose broker fails is reported and
    skipped; the others still trade.

    Scheduled runs are skipped (stored as SKIPPED) when the global switch is off or the automation is paused;
    manual runs ignore both. A run already in progress for the automation gives SKIPPED / OVERLAP.

    Args:
        ctx: app context.
        conn: app DB connection.
        automation: the automation dict (its configuration is copied into the run).
        trigger: "SCHEDULE" or "MANUAL".
        scheduled_for: the scheduled time (scheduled runs).
        schedule_id: the schedule that fired.
        fire_id: the fire record to link to the run.
        preview: plan and show, change nothing.

    Returns:
        The stored run (with result), or {"preview": True, "status", "error", "result"}.

    Used by: scheduler._tick_schedule, routes (run, preview, unsaved preview).
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = new_id("run")
    result = {
        "trigger": {"type": trigger, "scheduleId": schedule_id,
                    "scheduledFor": scheduled_for.astimezone(timezone.utc).isoformat() if scheduled_for else None, "fireId": fire_id},
        "configVersion": automation["configVersion"],
        "skipReason": None,
        "market": None,
        "budgets": [],
        "targets": [],
        "matched": [],
        "actions": [],
        "metrics": {},
    }
    tradings: list[tuple[dict, Trading | None]] = []  # (budget entry of the result, its Trading or None if it failed)
    live = LiveQuotes(ctx)  # live quotes fetched for this run, shared by every budget and order action

    def finish(status: str, error: str | None = None) -> dict:
        """Fill in the metrics and store (or, for previews, return) the run.

        Args:
            status: SUCCEEDED, PARTIAL, FAILED or SKIPPED.
            error: failure message, if any.
        """
        intents = [i for a in result["actions"] for b in a.get("budgets", []) for i in b.get("intents", [])]
        cost = lambda side, decision: sum(i["estimatedCost"] or 0 for i in intents  # noqa: E731
                                          if i["side"] == side and i["decision"] == decision)
        result["metrics"] = {
            "durationMs": int((time.monotonic() - started) * 1000),
            "matchedCount": len(result["matched"]),
            "actionCount": len(result["actions"]),
            "failedCount": sum(a["status"] == "FAILED" for a in result["actions"]),
            "budgetCount": len(result["budgets"]),
            "intentCount": len(intents),
            "placedCount": sum(i["decision"] == "PLACE" for i in intents),
            "skippedCount": sum(i["decision"] == "SKIP" for i in intents),
            "failedIntentCount": sum(i["decision"] == "FAILED" for i in intents),
            "estimatedBuyCost": round(cost("BUY", "PLACE"), 2),
            "estimatedSellProceeds": round(cost("SELL", "PLACE"), 2),
        }
        for entry, trading in tradings:
            if trading:
                entry["after"] = trading.budget_snapshot()
        if live.asked:
            result["liveQuotes"] = live.summary()
        if preview:
            return {"preview": True, "status": status, "error": error, "result": result}
        run = runs_store.add_run(conn, "AUTOMATION", automation["id"], automation, result["metrics"]["durationMs"],
                                 result=result, error=error, status=status,
                                 retention_days=ctx.settings.run_retention_days, run_id=run_id)
        for a in result["actions"]:
            if a.get("notificationId"):
                store.set_notification_run(conn, a["notificationId"], run["id"])
        if fire_id:
            store.attach_run(conn, fire_id, run["id"])
        return run

    if trigger == "SCHEDULE":
        if not store.get_switch(conn)["automationsEnabled"]:
            result["skipReason"] = "SWITCH_OFF"
            return finish("SKIPPED")
        if automation["status"] != "ACTIVE":
            result["skipReason"] = "PAUSED"
            return finish("SKIPPED")

    lock = _lock_for(automation["id"])
    if not lock.acquire(blocking=False):
        result["skipReason"] = "OVERLAP"
        return finish("SKIPPED")
    places_orders = any(a["type"] == "PLACE_ORDER" for a in automation["actions"])
    budgets = budget_store.budgets_of(conn, automation["id"]) if places_orders and automation.get("id") else []
    held_locks: list[threading.Lock] = []
    try:
        # One order-placing run per portfolio at a time; locks are taken in a fixed order so runs never deadlock.
        for pid in sorted({b["portfolioId"] for b in budgets}):
            portfolio_lock = _lock_for(f"portfolio:{pid}")
            if not portfolio_lock.acquire(blocking=True, timeout=120):
                result["skipReason"] = "OVERLAP"
                return finish("SKIPPED")
            held_locks.append(portfolio_lock)

        synced: set[str] = set()
        for b in budgets:
            entry = {"budgetId": b["id"], "name": b["name"], "portfolioId": b["portfolioId"]}
            try:
                trading = Trading(ctx, conn, automation, b, preview, synced)
                entry.update(brokerAccountId=trading.account["id"], environment=trading.account["environment"],
                             before=trading.budget_snapshot())
            except (BrokerError, ValueError) as e:
                trading = None
                entry["error"] = f"broker: {e}"
            result["budgets"].append(entry)
            tradings.append((entry, trading))

        result["market"] = market_context(ctx, now)
        try:
            result["targets"], symbols, ev = _run_targets(conn, ctx, automation)
            result["matched"] = ev.describe(symbols)
        except (EngineError, MarketError) as e:
            detail = e.detail if isinstance(e, MarketError) else str(e)
            return finish("FAILED", f"filters: {detail}")

        tickers = [m["ticker"] for m in result["matched"]]
        for index, action in enumerate(automation["actions"]):
            entry = {"index": index, "type": action["type"], "status": "SUCCEEDED"}
            try:
                if action["type"] == "NOTIFY":
                    if action.get("skipWhenNoMatches") and not tickers:
                        entry["status"] = "SKIPPED"
                    else:
                        title = _render(action["title"], automation, tickers)
                        message = _render(action.get("message", ""), automation, tickers)
                        shown = tickers if action.get("includeMatchedSymbols", True) else []
                        if preview:
                            entry["wouldNotify"] = {"title": title, "message": message, "symbols": shown}
                        else:
                            n = store.add_notification(conn, automation["id"], None, title, message, shown)
                            entry["notificationId"] = n["id"]
                elif action["type"] == "PLACE_ORDER":
                    _place_orders(ctx, conn, automation, tradings, PlaceOrderAction.model_validate(action), index,
                                  tickers, ev, result["market"], entry, run_id, preview, live)
                else:
                    raise ValueError(f"{action['type']} actions are not available yet")
            except Exception as e:  # one failing action does not stop the others
                log.exception("automation %s action %d failed", automation["id"], index)
                entry["status"] = "FAILED"
                entry["error"] = str(e)
            result["actions"].append(entry)

        failed = sum(a["status"] == "FAILED" for a in result["actions"])
        partial = sum(a["status"] == "PARTIAL" for a in result["actions"])
        if failed == len(result["actions"]):
            return finish("FAILED", "all actions failed")
        return finish("PARTIAL" if failed or partial else "SUCCEEDED")
    finally:
        for held in reversed(held_locks):
            held.release()
        lock.release()


class LiveQuotes:
    """Live quotes for one run: asked once per symbol, shared by every budget and order action.

    Attributes:
        quotes: ticker -> quote from MarketData.live_quotes (source IBKR or DELAYED).
        asked: tickers asked for so far.
        error: the market service's error, if the last request failed (orders then use the delayed price).

    Used by: run_automation (one per run; summary() goes into the run record), _place_orders.
    """

    def __init__(self, ctx: AppContext):
        """Args:
            ctx: app context (market helpers).
        """
        self.ctx = ctx
        self.quotes: dict[str, dict] = {}
        self.asked: set[str] = set()
        self.error: str | None = None

    def merge_into(self, snapshots: dict[str, dict], tickers: list[str]) -> None:
        """Fetch the tickers not asked yet and put their live prices into the planner's snapshots (in place).

        Args:
            snapshots: the PlanContext's ticker -> snapshot.
            tickers: symbols the plan would order.
        """
        missing = [t for t in dict.fromkeys(tickers) if t not in self.asked]
        if missing:
            self.asked.update(missing)
            try:
                self.quotes.update(self.ctx.market.live_quotes(set(missing)))
            except MarketError as e:
                self.error = e.detail
                log.warning("live quotes unavailable, using delayed prices: %s", e.detail)
        for t in tickers:
            q = self.quotes.get(t)
            if q and q.get("source") == "IBKR":
                snapshots[t] = {**snapshots.get(t, {}), "latestPrice": q["price"], "bid": q["bid"], "ask": q["ask"],
                                "priceSource": "IBKR", "quoteAgeSeconds": q.get("ageSeconds")}

    def summary(self) -> dict:
        """For the run record: how many symbols got a live quote, the error if any, and each quote used."""
        live = {t: q for t, q in self.quotes.items() if q.get("source") == "IBKR"}
        return {"asked": len(self.asked), "live": len(live), "error": self.error,
                "quotes": {t: {k: q.get(k) for k in ("source", "bid", "ask", "last", "time", "ageSeconds")}
                           for t, q in sorted(self.quotes.items())}}


def _place_orders(ctx, conn, automation, tradings: list[tuple[dict, "Trading | None"]], action: PlaceOrderAction,
                  index: int, tickers: list[str], ev, market: dict, entry: dict, run_id: str, preview: bool,
                  live: LiveQuotes | None = None) -> None:
    """For every budget of the automation: plan orders for the matched symbols with that budget's money and shares,
    and (unless previewing) submit the ones that pass every guard.

    Args:
        ctx: app context.
        conn: app DB connection.
        automation: the automation dict.
        tradings: (result budget entry, Trading or None when its broker failed) per budget.
        action: the PLACE_ORDER action.
        index: the action's position (part of each client order id).
        tickers: matched tickers.
        ev: the Evaluator (its cached snapshots are reused).
        market: market_context result.
        entry: the action's result entry: side, status, and per budget {budgetId, name, portfolioId, status, error,
            notHeld, intents}.
        run_id: the run's id (client order ids are <runId>-<index>-<budgetId>-<ticker>).
        preview: plan only.
        live: the run's live quotes: planner.select() picks the symbols, their live prices are fetched, and
            planner.size() prices them. None = delayed prices only.
    """
    entry["side"] = action.side
    entry["budgets"] = []
    if not tradings:
        entry["status"] = "SKIPPED"
        entry["detail"] = "the automation is not in any budget: add it to a budget on the portfolio page"
        return
    snapshots = ev._snaps(set(tickers))
    for budget_entry, trading in tradings:
        sub = {"budgetId": budget_entry["budgetId"], "name": budget_entry["name"],
               "portfolioId": budget_entry["portfolioId"], "status": "SUCCEEDED", "notHeld": 0, "intents": []}
        entry["budgets"].append(sub)
        if trading is None:
            sub.update(status="FAILED", error=budget_entry.get("error"))
            continue
        try:
            plan_ctx = trading.context(snapshots, market.get("session"), market.get("dataAgeMinutes"))
            for t, s in snapshots.items():
                plan_ctx.snapshots.setdefault(t, s)  # the context may come from an earlier action
            # Select which symbols to act on, quote exactly those live, then price, size and check them.
            chosen = planner.select(action, tickers, plan_ctx)
            if live is not None and not planner.market_blocked(trading.guards, plan_ctx):
                live.merge_into(plan_ctx.snapshots, chosen["symbols"][:100])
            out = {"intents": planner.size(action, trading.guards, chosen["symbols"], plan_ctx, trading.broker),
                   "notHeld": chosen["notHeld"]}
        except Exception as e:  # one budget failing does not stop the others
            log.exception("automation %s: planning for budget %s failed", automation["id"], trading.budget_id)
            sub.update(status="FAILED", error=str(e))
            continue
        sub["notHeld"], sub["intents"] = out["notHeld"], out["intents"]
        if preview:
            continue
        for intent in out["intents"]:
            if intent["decision"] != "PLACE":
                continue
            try:
                order = orders_service.place_order(
                    conn, trading.broker, trading.portfolio, symbol=intent["ticker"], side=action.side,
                    type=action.orderType, quantity=Decimal(str(intent["quantity"])), time_in_force=action.timeInForce,
                    limit_price=Decimal(str(intent["limitPrice"])) if intent["limitPrice"] is not None else None,
                    stop_price=Decimal(str(intent["stopPrice"])) if intent["stopPrice"] is not None else None,
                    trail_percent=action.trailPercent, extended_hours=action.extendedHours,
                    automation_id=automation["id"], run_id=run_id, budget_id=trading.budget_id,
                    client_order_id=f"{run_id}-{index}-{trading.budget_id}-{intent['ticker']}",
                    estimated_price=Decimal(str(intent["estimatedPrice"])))
            except orders_service.OrderRejected as e:
                intent.update(decision="SKIP", skipReason=e.reason, detail=str(e))
                continue
            intent["orderId"] = order["id"]
            intent["orderStatus"] = order["status"]
            if order["status"] == "REJECTED":
                intent.update(decision="FAILED", detail=order["rejectReason"])
        failed = sum(i["decision"] == "FAILED" for i in out["intents"])
        placed = sum(i["decision"] == "PLACE" for i in out["intents"])
        if failed:
            sub["status"] = "FAILED" if not placed else "PARTIAL"
    statuses = [b["status"] for b in entry["budgets"]]
    if all(st == "FAILED" for st in statuses):
        entry["status"] = "FAILED"
        entry["error"] = "; ".join(f"{b['name']}: {b.get('error') or 'orders failed'}" for b in entry["budgets"])
    elif any(st != "SUCCEEDED" for st in statuses):
        entry["status"] = "PARTIAL"
