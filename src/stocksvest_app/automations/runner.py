"""Run one automation: checks, market context, filter targets, actions, and the run record."""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

from ..deps import AppContext
from ..filters import store as filters_store
from ..filters.engine import EngineError, Evaluator
from ..filters.models import FilterIn
from ..market.client import MarketError
from ..runs import store as runs_store
from . import store
from .timing import ET

log = logging.getLogger(__name__)

# One run per automation at a time.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(automation_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(automation_id, threading.Lock())


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

    def finish(status: str, error: str | None = None) -> dict:
        result["metrics"] = {
            "durationMs": int((time.monotonic() - started) * 1000),
            "matchedCount": len(result["matched"]),
            "actionCount": len(result["actions"]),
            "failedCount": sum(a["status"] == "FAILED" for a in result["actions"]),
        }
        if preview:
            return {"preview": True, "status": status, "error": error, "result": result}
        run = runs_store.add_run(conn, "AUTOMATION", automation["id"], automation, result["metrics"]["durationMs"],
                                 result=result, error=error, status=status,
                                 retention_days=ctx.settings.run_retention_days)
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
    try:
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
                else:
                    raise ValueError(f"{action['type']} actions are not available yet")
            except Exception as e:  # one failing action does not stop the others
                log.exception("automation %s action %d failed", automation["id"], index)
                entry["status"] = "FAILED"
                entry["error"] = str(e)
            result["actions"].append(entry)

        failed = sum(a["status"] == "FAILED" for a in result["actions"])
        if failed == len(result["actions"]):
            return finish("FAILED", "all actions failed")
        return finish("PARTIAL" if failed else "SUCCEEDED")
    finally:
        lock.release()
