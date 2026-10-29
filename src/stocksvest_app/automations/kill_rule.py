"""The kill rule (docs/LIVE-RULES.md): when a portfolio falls KILL_DRAWDOWN_PERCENT below its peak, its buy
automations are paused. Its sell automations keep running and nothing is sold by this rule; resuming is manual.

Drawdown is measured on equity / allocated from the after-close snapshots, so money put in or taken out doesn't count
as gain or loss. It trips once per peak: after you resume, it trips again only after a new peak and a new fall.
"""

import json
import logging

from ..audit import store as audit
from ..budgets import store as budget_store
from ..portfolios import store as pf
from ..portfolios.store import D
from . import store

log = logging.getLogger(__name__)


def drawdown(conn, portfolio_id: str) -> tuple[float, float] | None:
    """(Drawdown percent from the peak, the peak's equity / allocated), from CLOSE snapshots; None without two.

    Args:
        conn: app DB connection.
        portfolio_id: the portfolio's id.

    Used by: check.
    """
    # ponytail: equity / allocated is not time-weighted; a big deposit near a peak shifts it a little.
    ratios = [D(r["equity"]) / D(r["allocated"]) for r in conn.execute(
        "SELECT equity, allocated FROM portfolio_snapshots WHERE portfolio_id = ? AND kind = 'CLOSE' ORDER BY at",
        (portfolio_id,)) if D(r["allocated"]) > 0]
    if len(ratios) < 2:
        return None
    peak = max(ratios)
    return float((1 - ratios[-1] / peak) * 100), float(peak)


def check(ctx, conn) -> list[dict]:
    """Pause the buy automations of every portfolio past the drawdown limit, once per peak, and email about it.

    Args:
        ctx: app context (settings, mailer).
        conn: app DB connection.

    Returns:
        [{portfolio, drawdownPercent, paused: [automation names]}] for the portfolios that tripped now.

    Used by: automations.scheduler.AutomationScheduler._loop.
    """
    limit = ctx.settings.kill_drawdown_percent
    tripped = []
    for p in pf.list_portfolios(conn) if limit > 0 else []:
        dd = drawdown(conn, p["id"])
        key = f"killrule:{p['id']}"
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        if not dd or dd[0] < limit or (row and json.loads(row["value"])["peak"] == dd[1]):
            continue
        paused = []
        for b in budget_store.list_budgets(conn, p["id"]):
            for ref in budget_store.automations_of(conn, b["id"]):
                a = store.get_automation(conn, ref["id"])
                buys = any(x["type"] == "PLACE_ORDER" and x["side"] == "BUY" for x in a["actions"])
                if buys and a["status"] == "ACTIVE":
                    store.set_status(conn, a["id"], "PAUSED")
                    paused.append(a["name"])
        with conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps({"peak": dd[1]})))
        audit.record(conn, "KILL_RULE", "PORTFOLIO", p["id"], source="SCHEDULER", outcome="FAILED",
                     details={"drawdownPercent": round(dd[0], 2), "limitPercent": limit, "paused": paused})
        log.warning("kill rule: %s is %.1f%% below its peak; paused %s", p["name"], dd[0], paused)
        if ctx.settings.alerts_enabled and ctx.mailer is not None and ctx.mailer.configured:
            from ..alerts import checks
            checks._send(conn, ctx.mailer, f"Kill rule: {p['name']} paused",
                         f"{p['name']} is {dd[0]:.1f}% below its peak (limit {limit:g}%).\n\n"
                         f"Paused buy automations: {', '.join(paused) or 'none were active'}.\n"
                         "Sell automations keep running; nothing was sold by this rule.\n\n"
                         "Follow docs/LIVE-RULES.md before resuming them.")
        tripped.append({"portfolio": p["name"], "drawdownPercent": round(dd[0], 2), "paused": paused})
    return tripped
