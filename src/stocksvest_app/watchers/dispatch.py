"""Acting on watcher events (WATCH-3): every new event is pushed to the phone, and runs the ACTIVE automations whose
eventTrigger matches it. PAPER runs right away; a run that places orders for a LIVE budget waits for a confirm
(POST /v1/events/{id}/confirm) and expires after the trigger's confirmWithinMinutes.
Design: UML/EventLifecycle.mmd, UML/WatchersIOVA.mmd.

Per matching automation, the event's `outcomes` keep {automationId, name, status, ...}: status is a run status
(SUCCEEDED, PARTIAL, FAILED, SKIPPED) or COOLDOWN, AWAITING_CONFIRM, DECLINED, EXPIRED.
"""

from datetime import datetime, timedelta, timezone

from .. import db
from ..alerts import push
from ..alerts.checks import _get, _put
from ..automations import store as automations_store
from ..automations.runner import run_automation
from ..brokers import store as brokers_store
from ..budgets import store as budget_store
from ..portfolios import store as portfolios_store
from . import store


def matches(trigger: dict, event: dict) -> bool:
    """Whether an automation's eventTrigger wants this event."""
    return ((not trigger.get("watchers") or event["watcher"] in trigger["watchers"])
            and (not trigger.get("kinds") or event["kind"] in trigger["kinds"])
            and event["severity"] >= trigger.get("minSeverity", 3))


def needs_confirm(conn, automation: dict) -> bool:
    """An automation that places orders for at least one budget on a LIVE broker account."""
    if not any(a["type"] == "PLACE_ORDER" for a in automation["actions"]):
        return False
    for b in budget_store.budgets_of(conn, automation["id"]):
        p = portfolios_store.get_portfolio(conn, b["portfolioId"])
        if p and brokers_store.get_account(conn, p["brokerAccountId"])["environment"] == "LIVE":
            return True
    return False


def _summary(outcomes: list[dict]) -> tuple[str, str]:
    """The event's status and reason from its outcomes."""
    states = [o["status"] for o in outcomes]
    if not outcomes:
        return "DONE", "no automation matched"
    if "AWAITING_CONFIRM" in states:
        return "AWAITING_CONFIRM", "waiting for a confirm on the phone"
    if "FAILED" in states:
        return "FAILED", "an automation run failed"
    for s in ("DECLINED", "EXPIRED"):
        if all(x == s for x in states):
            return s, f"{s.lower()} on the phone" if s == "DECLINED" else "not confirmed in time"
    return "DONE", ", ".join(f"{o['name']}: {o['status'].lower()}" for o in outcomes)


def _run(ctx, conn, automation_id: str, event: dict) -> dict:
    """Run one automation for an event; its outcome."""
    a = automations_store.get_automation(conn, automation_id)
    if not a:
        return {"automationId": automation_id, "name": "?", "status": "FAILED", "error": "automation deleted"}
    run = run_automation(ctx, conn, a, "EVENT", event=event)
    return {"automationId": a["id"], "name": a["name"], "status": run["status"], "runId": run["id"],
            "skipReason": (run.get("result") or {}).get("skipReason"), "error": run.get("error")}


def dispatch(ctx, conn, now: datetime | None = None) -> None:
    """Expire unanswered confirms, then handle NEW events, oldest first.

    Used by: thread.tick.
    """
    now = now or datetime.now(timezone.utc)
    expire(conn, now)
    autos = [a for a in automations_store.list_automations(conn, status="ACTIVE") if a.get("eventTrigger")]
    for ev in reversed(store.list_events(conn, status="NEW", limit=200)):
        if not store.claim(conn, ev["id"], "NEW"):  # ponytail: a crash mid-event leaves it ACTING, never runs it twice
            continue
        outcomes = []
        for a in autos:
            t = a["eventTrigger"]
            if not matches(t, ev):
                continue
            last = (_get(conn, f"event-run:{a['id']}") or {}).get("at")
            if last and now - datetime.fromisoformat(last) < timedelta(minutes=t.get("cooldownMinutes", 60)):
                outcomes.append({"automationId": a["id"], "name": a["name"], "status": "COOLDOWN"})
                continue
            _put(conn, f"event-run:{a['id']}", {"at": now.isoformat()})  # the cooldown starts at the match
            if needs_confirm(conn, a):
                outcomes.append({"automationId": a["id"], "name": a["name"], "status": "AWAITING_CONFIRM",
                                 "expiresAt": db.iso(now + timedelta(minutes=t.get("confirmWithinMinutes", 15)))})
            else:
                outcomes.append(_run(ctx, conn, a["id"], ev))
        status, reason = _summary(outcomes)
        store.set_status(conn, ev["id"], status, reason, outcomes)
        if ctx.settings.alerts_enabled:
            body = ev["body"]
            waiting = [o for o in outcomes if o["status"] == "AWAITING_CONFIRM"]
            ran = [o for o in outcomes if o.get("runId")]
            if waiting:
                body += f" Confirm in the app: {', '.join(o['name'] for o in waiting)}."
            if ran:
                body += f" Ran: {', '.join(o['name'] + ' (' + o['status'].lower() + ')' for o in ran)}."
            push.send(conn, "events", ev["title"], body)


def confirm(ctx, conn, event_id: str, yes: bool) -> dict | None:
    """Answer a LIVE confirm: run (yes) or decline the automations waiting on this event. Expired ones are not run.

    Returns:
        The event, or None when it isn't waiting for a confirm (unknown, already answered or expired).

    Used by: routes (POST /v1/events/{id}/confirm).
    """
    if not store.claim(conn, event_id, "AWAITING_CONFIRM"):
        return None
    ev = store.get_event(conn, event_id)
    now = db.now_iso()
    outcomes = []
    for o in ev["outcomes"]:
        if o["status"] == "AWAITING_CONFIRM":
            if o["expiresAt"] < now:
                o = {**o, "status": "EXPIRED"}
            elif yes:
                o = _run(ctx, conn, o["automationId"], ev)
            else:
                o = {**o, "status": "DECLINED"}
        outcomes.append(o)
    store.set_status(conn, event_id, *_summary(outcomes), outcomes)
    return store.get_event(conn, event_id)


def expire(conn, now: datetime) -> None:
    """Mark confirms not given in time as EXPIRED.

    Used by: dispatch.
    """
    for ev in store.list_events(conn, status="AWAITING_CONFIRM", limit=200):
        waiting = [o for o in ev["outcomes"] if o["status"] == "AWAITING_CONFIRM"]
        if all(o["expiresAt"] < db.iso(now) for o in waiting) and store.claim(conn, ev["id"], "AWAITING_CONFIRM"):
            outcomes = [{**o, "status": "EXPIRED"} if o["status"] == "AWAITING_CONFIRM" else o for o in ev["outcomes"]]
            store.set_status(conn, ev["id"], *_summary(outcomes), outcomes)
