"""What the app watches and emails about. Run from the scheduler loop; does real work every CHECK_EVERY.

Problems (one email when it starts, after its grace period, and one when it is resolved):
  broker:<account>   the broker sync failed, or found differences with the broker
  market:reachable   the market service doesn't answer
  market:sync        the market service reports a sync error
  market:stale       during the regular session, the market data hasn't synced for STALE_AFTER
  market:ibkr        during the regular session, live quotes (IBKR) are disconnected
Events (one email per check with everything new):
  automation runs that ended FAILED or PARTIAL
Daily:
  a summary SUMMARY_AFTER the regular close: portfolios, budgets, runs, orders and the open problems.

State lives in app_settings (keys "alert:*"), so a restart neither repeats nor forgets an alert.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..audit import store as audit
from ..brokers import store as brokers_store
from ..budgets import store as budget_store
from ..db import iso
from ..market.client import MarketError
from ..orders import store as orders_store
from ..portfolios import store as portfolios_store
from ..runs import store as runs_store

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
CHECK_EVERY = timedelta(minutes=5)          # Used by: check
STALE_AFTER = timedelta(minutes=30)         # market data older than this during the session. Used by: check
GRACE = timedelta(minutes=10)               # how long a market problem must last before an email. Used by: check
SUMMARY_AFTER = timedelta(minutes=30)       # after the regular close. Used by: check


# ------------------------------------------------------------------ state


def _get(conn, key: str) -> dict | None:
    """One alert state from app_settings, or None.

    Args:
        conn: app DB connection.
        key: e.g. "alert:market:ibkr".
    """
    r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else None


def _put(conn, key: str, value: dict | None) -> None:
    """Store (or, for None, delete) one alert state.

    Args:
        conn: app DB connection.
        key: the state key.
        value: JSON-able dict, or None to delete.
    """
    with conn:
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def active(conn) -> list[dict]:
    """The problems that are currently reported (emailed and not resolved yet).

    Args:
        conn: app DB connection.

    Returns:
        [{key, title, detail, since}].

    Used by: summary, tests.
    """
    rows = conn.execute("SELECT key, value FROM app_settings WHERE key LIKE 'alert:problem:%'").fetchall()
    out = []
    for r in rows:
        v = json.loads(r["value"])
        if v.get("sent"):
            out.append({"key": r["key"].removeprefix("alert:problem:"), "title": v["title"], "detail": v["detail"],
                        "since": v["since"]})
    return sorted(out, key=lambda x: x["since"])


def _send(conn, mailer, subject: str, text: str) -> bool:
    """Send an email and log it in the activity log. A failed send is logged and reported as False (retried later).

    Args:
        conn: app DB connection.
        mailer: the Mailer.
        subject: subject line.
        text: body.
    """
    try:
        mailer.send(subject, text)
    except Exception as e:  # the relay is down or refused: try again at the next check
        log.warning("alert email failed (%s): %s", subject, e)
        audit.record(conn, "ALERT_EMAIL_FAILED", source="SYSTEM", outcome="FAILED", error=str(e),
                     details={"subject": subject})
        return False
    audit.record(conn, "ALERT_EMAIL_SENT", source="SYSTEM", details={"subject": subject})
    return True


def condition(conn, mailer, key: str, title: str, problem: str | None, now: datetime,
              grace: timedelta = timedelta(0)) -> None:
    """Track one problem: email once when it has lasted `grace`, and once more when it is gone.

    Args:
        conn: app DB connection.
        mailer: the Mailer.
        key: stable name, e.g. "market:ibkr".
        title: readable name for the email.
        problem: what is wrong now, or None when all is well.
        now: check time.
        grace: how long the problem must last before it is emailed.

    Used by: check.
    """
    skey = f"alert:problem:{key}"
    state = _get(conn, skey)
    if problem:
        state = state or {"since": iso(now), "sent": False, "title": title}
        state["detail"] = problem
        if not state["sent"] and now - datetime.fromisoformat(state["since"]) >= grace:
            state["sent"] = _send(conn, mailer, f"Problem: {title}",
                                  f"{title}\n\n{problem}\n\nSince {state['since']}. You'll get another email when it's resolved.")
        _put(conn, skey, state)
    elif state:
        if state.get("sent"):
            lasted = now - datetime.fromisoformat(state["since"])
            if not _send(conn, mailer, f"Resolved: {title}",
                         f"{title} is back to normal (the problem lasted about {int(lasted.total_seconds() // 60)} min).\n\n"
                         f"It was: {state['detail']}"):
                return  # keep the state; the "resolved" email is retried at the next check
        _put(conn, skey, None)


# ------------------------------------------------------------------ the checks


def _failed_runs(conn, since: str) -> list[str]:
    """Readable lines for automation runs that ended FAILED or PARTIAL at or after `since`.

    Args:
        conn: app DB connection.
        since: ISO time.
    """
    lines = []
    for r in reversed(runs_store.list_runs(conn, "AUTOMATION", since=since, limit=200, include_result=True)):
        if r["status"] not in ("FAILED", "PARTIAL"):
            continue
        name = (r.get("definition") or {}).get("name", r["subjectId"])
        why = r.get("error") or "; ".join(
            f"{b['name']}: {b.get('error') or 'some orders failed'}"
            for a in (r.get("result") or {}).get("actions", []) for b in a.get("budgets", [])
            if b.get("status") in ("FAILED", "PARTIAL")) or "see the run"
        lines.append(f"- {r['ranAt']}  {name}: {r['status']}. {why}  (run {r['id']})")
    return lines


def check(ctx, conn, now: datetime | None = None) -> None:
    """Run the checks (at most every CHECK_EVERY) and send what is due. Does nothing unless ALERTS_ENABLED and the
    mailer is configured.

    Args:
        ctx: app context (settings, mailer, market API and helpers).
        conn: app DB connection.
        now: check time (tests pin it); default now.

    Used by: automations.scheduler.AutomationScheduler._loop.
    """
    if not ctx.settings.alerts_enabled or ctx.mailer is None or not ctx.mailer.configured:
        return
    now = now or datetime.now(timezone.utc)
    last = _get(conn, "alert:last_check")
    if last and now - datetime.fromisoformat(last["at"]) < CHECK_EVERY:
        return
    since = last["at"] if last else iso(now - CHECK_EVERY)
    _put(conn, "alert:last_check", {"at": iso(now)})
    mailer = ctx.mailer

    # Automation runs that failed since the last check.
    lines = _failed_runs(conn, since)
    if lines:
        _send(conn, mailer, f"{len(lines)} automation run(s) failed", "\n".join(lines))

    # Broker accounts: the last sync.
    for account in brokers_store.list_accounts(conn):
        last_sync = orders_store.last_sync(conn, account["id"])
        problem = None
        if last_sync and last_sync["error"]:
            problem = f"The broker sync failed: {last_sync['error']}"
        elif last_sync and last_sync["mismatches"]:
            problem = "The app and the broker disagree:\n" + "\n".join(
                f"- {m.get('kind')} {m.get('symbol') or ''}: app {m.get('appValue')}, broker {m.get('brokerValue')}. "
                f"{m.get('detail') or ''}" for m in last_sync["mismatches"])
        condition(conn, mailer, f"broker:{account['id']}", f"Broker account {account['name']}", problem, now)

    # The market service, its sync and the live quotes.
    today = now.astimezone(ET).date().isoformat()
    try:
        status = ctx.market_api.status()
        day = ctx.market.trading_day(today)
    except MarketError as e:
        condition(conn, mailer, "market:reachable", "Market service", f"It doesn't answer: {e.detail}", now, GRACE)
        return
    condition(conn, mailer, "market:reachable", "Market service", None, now)
    in_session = bool(day) and datetime.fromisoformat(day["regularOpen"]) <= now < datetime.fromisoformat(day["regularClose"])
    condition(conn, mailer, "market:sync", "Market data sync",
              f"The last sync failed: {status['lastError']}" if status.get("lastError") else None, now, GRACE)
    synced = status.get("lastIncrementalAt")
    stale = in_session and (not synced or now - datetime.fromisoformat(synced) > STALE_AFTER)
    condition(conn, mailer, "market:stale", "Market data",
              f"No sync since {synced or 'ever'} during the regular session." if stale else None, now)
    live = status.get("liveQuotes") or {}
    ibkr_down = in_session and live.get("enabled") and not live.get("connected")
    condition(conn, mailer, "market:ibkr", "Live prices",
              f"The live quote source fails ({live.get('lastError') or 'no detail'}). Orders use the newest "
              f"minute bar meanwhile, and requireLivePrice orders are skipped." if ibkr_down else None, now, GRACE)

    # The daily summary, once per trading day after the close.
    if day and now >= datetime.fromisoformat(day["regularClose"]) + SUMMARY_AFTER:
        if (_get(conn, "alert:summary") or {}).get("date") != today:
            if _send(conn, mailer, f"Daily summary {today}", summary(ctx, conn, today)):
                _put(conn, "alert:summary", {"date": today})


def summary(ctx, conn, day: str) -> str:
    """The daily summary: portfolios, budgets, today's automation runs and orders, and the open problems.

    Args:
        ctx: app context.
        conn: app DB connection.
        day: New York trading date, YYYY-MM-DD.

    Used by: check.
    """
    start = datetime.fromisoformat(day).replace(tzinfo=ET)
    lo, hi = iso(start), iso(start + timedelta(days=1))
    out = [f"StocksVest, {day}", ""]

    for p in portfolios_store.list_portfolios(conn):
        out.append(f"Portfolio {p['name']}: cash {p['cash']:,.2f} of {p['allocated']:,.2f} capital")
        for b in budget_store.list_budgets(conn, p["id"]):
            names = ", ".join(a["name"] for a in b["automations"]) or "no automations"
            out.append(f"  Budget {b['name']} ({names}): cash {b['cash']:,.2f}, invested {b['invested']:,.2f}, "
                       f"realized P&L {b['realizedPnL']:+,.2f}, fees {b['fees']:,.2f}")
    out.append("")

    runs = [r for r in runs_store.list_runs(conn, "AUTOMATION", since=lo, limit=5000) if r["ranAt"] < hi]
    names = {a: (runs_store.get_run(conn, rid) or {}).get("definition", {}).get("name", a)
             for a, rid in {r["subjectId"]: r["id"] for r in runs}.items()}
    per = Counter((names[r["subjectId"]], r["status"]) for r in runs)
    out.append(f"Automation runs: {len(runs)}")
    for name in sorted({n for n, _ in per}):
        counts = ", ".join(f"{st.lower()} {c}" for (n, st), c in sorted(per.items()) if n == name)
        out.append(f"  {name}: {counts}")
    out.append("")

    orders = conn.execute("SELECT side, status, COUNT(*) AS n FROM orders WHERE created_at >= ? AND created_at < ? "
                          "GROUP BY side, status", (lo, hi)).fetchall()
    fills = conn.execute("SELECT side, COUNT(*) AS n, SUM(CAST(quantity AS REAL) * CAST(price AS REAL)) AS value, "
                         "SUM(CAST(COALESCE(realized_pnl, '0') AS REAL)) AS pnl FROM fills "
                         "WHERE executed_at >= ? AND executed_at < ? GROUP BY side", (lo, hi)).fetchall()
    out.append("Orders: " + (", ".join(f"{r['side'].lower()} {r['status'].lower()} {r['n']}" for r in orders) or "none"))
    for f in fills:
        extra = f", realized P&L {f['pnl']:+,.2f}" if f["side"] == "SELL" else ""
        out.append(f"  {f['side'].lower()} fills: {f['n']}, {f['value']:,.2f}{extra}")
    out.append("")

    problems = active(conn)
    out.append("Open problems: " + ("none" if not problems else ""))
    out += [f"  {p['title']}: {p['detail']} (since {p['since']})" for p in problems]
    return "\n".join(out)
