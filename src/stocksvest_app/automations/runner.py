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
from . import budget, planner
from .models import OrderGuards, PlaceOrderAction
from ..filters import store as filters_store
from ..filters.engine import EngineError, Evaluator
from ..filters.models import FilterIn
from ..market.client import MarketError
from ..runs import store as runs_store
from . import store
from .timing import ET

log = logging.getLogger(__name__)

# One run per automation at a time, and one order-placing run per portfolio at a time.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class Trading:
    """Portfolio, broker and planning state for one run of an automation that places orders."""

    def __init__(self, ctx: AppContext, conn, automation: dict, preview: bool):
        self.conn, self.automation = conn, automation
        self.portfolio = portfolios_store.get_portfolio(conn, automation["portfolioId"])
        if not self.portfolio:
            raise ValueError("the automation's portfolio no longer exists")
        self.account = brokers_store.get_account(conn, self.portfolio["brokerAccountId"])
        self.broker = ctx.broker_factory(self.account)
        if not preview:  # current fills and balances before deciding anything
            orders_service.sync_portfolio(conn, self.broker, self.portfolio)
            self.portfolio = portfolios_store.get_portfolio(conn, self.portfolio["id"])
        self.guards = OrderGuards.model_validate(automation.get("orderGuards") or {})
        self.plan_ctx: planner.PlanContext | None = None

    def pool(self) -> dict | None:
        return budget.get_pool(self.conn, self.automation["id"])

    def budget_snapshot(self) -> dict | None:
        p = self.pool()
        if not p:
            return None
        return {k: p[k] for k in ("cash", "reserved", "available", "invested", "realizedPnL", "fees", "allocated")}

    def context(self, snapshots: dict, session, data_age) -> planner.PlanContext:
        if self.plan_ctx is None:
            aid, pid = self.automation["id"], self.portfolio["id"]
            holdings = {p["symbol"]: planner.Holding(p["quantity"], p["costBasis"])
                        for p in portfolios_store.positions(self.conn, pid, automation_id=aid)}
            open_orders = orders_store.list_orders(self.conn, pid, automation_id=aid, open_only=True, limit=1000)
            open_sells: dict[str, Decimal] = {}
            for o in open_orders:
                if o["side"] == "SELL":
                    open_sells[o["symbol"]] = open_sells.get(o["symbol"], Decimal(0)) + Decimal(
                        str(o["quantity"] - o["filledQuantity"]))
            pool = self.pool()
            self.plan_ctx = planner.PlanContext(
                snapshots=snapshots, holdings=holdings,
                open_buys={o["symbol"] for o in open_orders if o["side"] == "BUY"}, open_sells=open_sells,
                budget_available=Decimal(str(pool["available"])) if pool else Decimal(0),
                budget_equity=Decimal(str(pool["cash"] + pool["invested"])) if pool else Decimal(0),
                session=session, data_age_minutes=data_age)
        return self.plan_ctx


def market_context(ctx: AppContext, now: datetime) -> dict:
    """Which session the market is in and how old the synced data is."""
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
    return text.replace("{name}", automation["name"]).replace("{count}", str(len(symbols)))


def _run_targets(conn, ctx: AppContext, automation: dict) -> tuple[list[dict], set[str], Evaluator]:
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
    a preview returns the same shape without storing anything or creating notifications."""
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = new_id("run")
    result = {
        "trigger": {"type": trigger, "scheduleId": schedule_id,
                    "scheduledFor": scheduled_for.astimezone(timezone.utc).isoformat() if scheduled_for else None, "fireId": fire_id},
        "configVersion": automation["configVersion"],
        "skipReason": None,
        "market": None,
        "targets": [],
        "matched": [],
        "actions": [],
        "metrics": {},
    }
    trading: Trading | None = None

    def finish(status: str, error: str | None = None) -> dict:
        intents = [i for a in result["actions"] for i in a.get("intents", [])]
        cost = lambda side, decision: sum(i["estimatedCost"] or 0 for i in intents  # noqa: E731
                                          if i["side"] == side and i["decision"] == decision)
        result["metrics"] = {
            "durationMs": int((time.monotonic() - started) * 1000),
            "matchedCount": len(result["matched"]),
            "actionCount": len(result["actions"]),
            "failedCount": sum(a["status"] == "FAILED" for a in result["actions"]),
            "intentCount": len(intents),
            "placedCount": sum(i["decision"] == "PLACE" for i in intents),
            "skippedCount": sum(i["decision"] == "SKIP" for i in intents),
            "failedIntentCount": sum(i["decision"] == "FAILED" for i in intents),
            "estimatedBuyCost": round(cost("BUY", "PLACE"), 2),
            "estimatedSellProceeds": round(cost("SELL", "PLACE"), 2),
        }
        if trading:
            result["budgetAfter"] = trading.budget_snapshot()
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
    portfolio_lock = _lock_for(f"portfolio:{automation.get('portfolioId')}") if places_orders else None
    if portfolio_lock and not portfolio_lock.acquire(blocking=True, timeout=120):
        lock.release()
        result["skipReason"] = "OVERLAP"
        return finish("SKIPPED")
    try:
        if places_orders:
            try:
                trading = Trading(ctx, conn, automation, preview)
            except (BrokerError, ValueError) as e:
                return finish("FAILED", f"broker: {e}")
            result.update(portfolioId=trading.portfolio["id"], brokerAccountId=trading.account["id"],
                          environment=trading.account["environment"], budgetBefore=trading.budget_snapshot())
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
                    _place_orders(ctx, conn, automation, trading, PlaceOrderAction.model_validate(action), index,
                                  tickers, ev, result["market"], entry, run_id, preview)
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
        if portfolio_lock:
            portfolio_lock.release()
        lock.release()


def _place_orders(ctx, conn, automation, trading: Trading, action: PlaceOrderAction, index: int, tickers: list[str],
                  ev, market: dict, entry: dict, run_id: str, preview: bool) -> None:
    """Plan orders for the matched symbols and (unless previewing) submit the ones that pass every guard."""
    if not trading.pool():
        raise ValueError("this automation has no budget; allocate money to it first")
    plan_ctx = trading.context(ev._snaps(set(tickers)), market.get("session"), market.get("dataAgeMinutes"))
    out = planner.plan(action, trading.guards, tickers, plan_ctx, trading.broker)
    entry["side"] = action.side
    entry["notHeld"] = out["notHeld"]
    entry["intents"] = out["intents"]
    if preview:
        return
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
                automation_id=automation["id"], run_id=run_id,
                client_order_id=f"{run_id}-{index}-{intent['ticker']}",
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
        entry["status"] = "FAILED" if not placed else "PARTIAL"
