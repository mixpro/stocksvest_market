"""/v1/automations and /v1/notifications."""

import sqlite3
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, TypeAdapter

from ..db import new_id
from ..deps import AppContext
from ..filters import store as filters_store
from ..market.client import MarketError
from ..runs import store as runs_store
from . import store
from ..portfolios import store as portfolios_store
from ..orders import service as orders_service
from . import budget
from .models import AutomationIn, BudgetChangeIn, Schedule, SwitchIn
from .runner import run_automation
from .timing import next_fires

_schedule = TypeAdapter(Schedule)


class SchedulePreviewIn(BaseModel):
    schedules: list[Schedule] = Field(min_length=1)


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, automation_id: str) -> dict:
        a = store.get_automation(conn, automation_id)
        if not a:
            raise HTTPException(404, f"automation '{automation_id}' not found")
        return a

    def prepare(conn, body: AutomationIn) -> dict:
        """Check references and give every schedule a stable id."""
        if body.portfolioId and not portfolios_store.get_portfolio(conn, body.portfolioId):
            raise HTTPException(422, f"portfolio '{body.portfolioId}' does not exist")
        for t in body.runTargets:
            exists = filters_store.get_filter if t.type.value == "FILTER" else filters_store.get_group
            if not exists(conn, t.id):
                raise HTTPException(422, f"{t.type.value.lower().replace('_', ' ')} '{t.id}' does not exist")
        data = body.model_dump(mode="json")
        seen = set()
        for s in data["schedules"]:
            if not s.get("id") or s["id"] in seen:
                s["id"] = new_id("sch")
            seen.add(s["id"])
        return data

    def upcoming(conn, automation_id: str, schedules: list[dict], created: datetime, count: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        try:
            for raw in schedules:
                s = _schedule.validate_python(raw)
                fired = store.fire_count(conn, automation_id, s.id) if automation_id else 0
                times = next_fires(s, now, created, ctx.market.calendar_days, limit=count, fired_count=fired)
                out.append({"scheduleId": s.id, "type": s.type, "next": [t.isoformat() for t in times]})
        except MarketError as e:
            raise HTTPException(e.status if e.status < 500 else 502, e.detail)
        return out

    def with_status(conn, a: dict) -> dict:
        """Add when it runs next and how the last run went, so clients can show both at a glance."""
        next_at = None
        if a["status"] == "ACTIVE":
            created = datetime.fromisoformat(a["createdAt"])
            for raw in a["schedules"]:
                state = store.get_state(conn, raw["id"])
                if state is not None:  # kept current by the scheduler
                    t = datetime.fromisoformat(state["next_fire_at"]) if state["next_fire_at"] else None
                else:  # just saved or resumed: the scheduler hasn't planned it yet
                    try:
                        s = _schedule.validate_python(raw)
                        nxt = next_fires(s, datetime.now(timezone.utc), created, ctx.market.calendar_days,
                                         fired_count=store.fire_count(conn, a["id"], s.id))
                    except MarketError:
                        nxt = []
                    t = nxt[0] if nxt else None
                if t and (next_at is None or t < next_at):
                    next_at = t
        last = runs_store.list_runs(conn, "AUTOMATION", a["id"], limit=1)
        return {
            **a,
            "nextRunAt": next_at.astimezone(timezone.utc).isoformat() if next_at else None,
            "lastRun": {k: last[0][k] for k in ("id", "ranAt", "status", "error")} if last else None,
        }

    # ---------------------------------------------------------------- global switch

    @r.get("/v1/automations/switch", tags=["automations"])
    def get_switch(conn: Conn):
        """The global on/off switch for scheduled runs, plus scheduler health."""
        sched = ctx.scheduler
        return {
            **store.get_switch(conn),
            "scheduler": {
                "running": sched is not None,
                "lastTickAt": sched.last_tick_at.isoformat() if sched and sched.last_tick_at else None,
                "lastError": sched.last_error if sched else None,
            },
        }

    @r.put("/v1/automations/switch", tags=["automations"])
    def set_switch(body: SwitchIn, conn: Conn):
        """Turn all scheduled runs off (they are recorded as SKIPPED / SWITCH_OFF) or back on."""
        return store.set_switch(conn, body.automationsEnabled, body.reason)

    # ---------------------------------------------------------------- previews without saving

    @r.post("/v1/automations/preview", tags=["automations"])
    def preview_unsaved(body: AutomationIn, conn: Conn):
        """Run an unsaved automation: filters run and actions are shown, nothing is stored or sent."""
        data = prepare(conn, body)
        draft = {"id": "preview", "status": "ACTIVE", "configVersion": 0, **data}
        return run_automation(ctx, conn, draft, "MANUAL", preview=True)

    @r.post("/v1/automations/schedule-preview", tags=["automations"])
    def schedule_preview(body: SchedulePreviewIn, conn: Conn, count: Annotated[int, Query(ge=1, le=50)] = 5):
        """Next fire times for schedules being edited."""
        schedules = [dict(s.model_dump(mode="json"), id=s.id or f"draft{i}") for i, s in enumerate(body.schedules)]
        return {"schedules": upcoming(conn, "", schedules, datetime.now(timezone.utc), count)}

    # ---------------------------------------------------------------- automations

    @r.get("/v1/automations", tags=["automations"])
    def list_automations(conn: Conn, status: str | None = None):
        return {"automations": [with_status(conn, a) for a in store.list_automations(conn, status)]}

    @r.post("/v1/automations", tags=["automations"], status_code=201)
    def create_automation(body: AutomationIn, conn: Conn):
        """New automations start ACTIVE; their schedules start counting from now."""
        return store.create_automation(conn, prepare(conn, body))

    @r.get("/v1/automations/{automation_id}", tags=["automations"])
    def get_automation(automation_id: str, conn: Conn):
        """Includes nextRunAt (null when paused or nothing is coming) and lastRun."""
        return with_status(conn, load(conn, automation_id))

    @r.put("/v1/automations/{automation_id}", tags=["automations"])
    def update_automation(automation_id: str, body: AutomationIn, conn: Conn):
        """Replace the definition. configVersion goes up and schedules restart from now."""
        load(conn, automation_id)
        return store.update_automation(conn, automation_id, prepare(conn, body))

    @r.delete("/v1/automations/{automation_id}", tags=["automations"], status_code=204)
    def delete_automation(automation_id: str, conn: Conn,
                          return_budget: Annotated[bool, Query(alias="returnBudget")] = False):
        """Deletes the automation; its run history and budget ledger stay. Refused while it holds shares or has
        open orders. Budget cash must go back to the portfolio first: withdraw it, or pass returnBudget=true."""
        load(conn, automation_id)
        reasons = budget.holdings_blocking_removal(conn, automation_id)
        if reasons:
            raise HTTPException(409, "can't delete this automation: " + "; ".join(reasons))
        pool = budget.get_pool(conn, automation_id)
        if pool and pool["cash"] and not return_budget:
            raise HTTPException(409, f"can't delete this automation: its budget still holds {pool['cash']:.2f}; "
                                     "withdraw it first or delete with returnBudget=true")
        with conn:
            if pool:
                budget.close_pool(conn, automation_id, "returned to the portfolio: the automation was deleted")
        store.delete_automation(conn, automation_id)

    @r.post("/v1/automations/{automation_id}/pause", tags=["automations"])
    def pause(automation_id: str, conn: Conn):
        load(conn, automation_id)
        return store.set_status(conn, automation_id, "PAUSED")

    @r.post("/v1/automations/{automation_id}/resume", tags=["automations"])
    def resume(automation_id: str, conn: Conn):
        """Resume from now: times missed while paused are not run."""
        load(conn, automation_id)
        return store.set_status(conn, automation_id, "ACTIVE")

    @r.post("/v1/automations/{automation_id}/preview", tags=["automations"])
    def preview(automation_id: str, conn: Conn):
        """What a run would do now. Nothing is stored or sent."""
        return run_automation(ctx, conn, load(conn, automation_id), "MANUAL", preview=True)

    @r.post("/v1/automations/{automation_id}/run", tags=["automations"])
    def run_now(automation_id: str, conn: Conn):
        """Run now (also when paused or switched off) and store the run."""
        return run_automation(ctx, conn, load(conn, automation_id), "MANUAL")

    @r.get("/v1/automations/{automation_id}/runs", tags=["automations"])
    def runs(automation_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 20):
        return {"runs": runs_store.list_runs(conn, "AUTOMATION", automation_id, limit=limit)}

    @r.get("/v1/automations/{automation_id}/next-runs", tags=["automations"])
    def next_runs(automation_id: str, conn: Conn, count: Annotated[int, Query(ge=1, le=50)] = 5):
        """Upcoming fire times per schedule (empty while paused)."""
        a = load(conn, automation_id)
        if a["status"] != "ACTIVE":
            return {"status": a["status"], "schedules": []}
        return {"status": a["status"],
                "schedules": upcoming(conn, a["id"], a["schedules"], datetime.fromisoformat(a["createdAt"]), count)}

    @r.get("/v1/automations/{automation_id}/events", tags=["automations"])
    def events(automation_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 100):
        """Change log: created, updated (with what changed), paused, resumed."""
        return {"events": store.list_events(conn, automation_id, limit)}

    # ---------------------------------------------------------------- budget pool

    def budget_view(conn, automation_id: str) -> dict:
        return {"pool": budget.get_pool(conn, automation_id), "entries": budget.entries(conn, automation_id)}

    @r.get("/v1/automations/{automation_id}/budget", tags=["automations"])
    def get_budget(automation_id: str, conn: Conn):
        """The automation's money (cash, reserved, available, invested, P&L) and its ledger, newest first."""
        load(conn, automation_id)
        return budget_view(conn, automation_id)

    @r.post("/v1/automations/{automation_id}/budget/allocate", tags=["automations"])
    def allocate(automation_id: str, body: BudgetChangeIn, conn: Conn):
        """Move money from the portfolio's unallocated cash into this automation's budget."""
        a = load(conn, automation_id)
        if not a.get("portfolioId"):
            raise HTTPException(422, "set the automation's portfolioId first")
        portfolio = portfolios_store.get_portfolio(conn, a["portfolioId"])
        try:
            budget.allocate(conn, automation_id, portfolio, body.amount, body.note,
                            orders_service.open_manual_buys(conn, portfolio["id"]))
        except budget.BudgetError as e:
            raise HTTPException(422, str(e))
        with conn:
            store.add_event(conn, automation_id, "BUDGET_ALLOCATED", a["configVersion"], {"amount": str(body.amount)})
        return budget_view(conn, automation_id)

    @r.post("/v1/automations/{automation_id}/budget/withdraw", tags=["automations"])
    def withdraw(automation_id: str, body: BudgetChangeIn, conn: Conn):
        """Give available (not reserved, not invested) budget back to the portfolio."""
        a = load(conn, automation_id)
        try:
            budget.withdraw(conn, automation_id, body.amount, body.note)
        except budget.BudgetError as e:
            raise HTTPException(422, str(e))
        with conn:
            store.add_event(conn, automation_id, "BUDGET_WITHDRAWN", a["configVersion"], {"amount": str(body.amount)})
        return budget_view(conn, automation_id)

    # ---------------------------------------------------------------- notifications

    @r.get("/v1/notifications", tags=["notifications"])
    def notifications(conn: Conn, unread_only: Annotated[bool, Query(alias="unreadOnly")] = False,
                      limit: Annotated[int, Query(ge=1, le=500)] = 50):
        return {"unread": store.unread_count(conn), "notifications": store.list_notifications(conn, unread_only, limit)}

    @r.post("/v1/notifications/read-all", tags=["notifications"])
    def read_all(conn: Conn):
        return {"marked": store.mark_read(conn)}

    @r.post("/v1/notifications/{notification_id}/read", tags=["notifications"])
    def read_one(notification_id: str, conn: Conn):
        if not store.mark_read(conn, notification_id) and not conn.execute(
                "SELECT 1 FROM notifications WHERE id = ?", (notification_id,)).fetchone():
            raise HTTPException(404, f"notification '{notification_id}' not found")
        return {"marked": 1}

    return r
