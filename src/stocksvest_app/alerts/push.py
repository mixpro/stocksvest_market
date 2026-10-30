"""Push notifications to the phone app through the Expo push service (it hands them to Apple / Google).

The phone registers its Expo push token (POST /v1/push/devices); each category can be switched off.
Two sources:
  - the email alerts (checks._send with a category): problems, negative cash, failed runs, halal, daily summary
  - activity log entries, picked up every scheduler tick (events): fills, rejections, kill rule / kill all, money

State lives in app_settings: "push:devices" {tokens}, "push:off" {categories}, "push:cursor" {id}.
"""

import json
import logging

import requests

from .checks import _get, _put

log = logging.getLogger(__name__)
EXPO_URL = "https://exp.host/--/api/v2/push/send"

CATEGORIES = {  # key: label. All on until switched off. Used by: routes, send
    "fills": "Order filled",
    "rejected": "Order rejected",
    "kill": "Kill rule and Kill all",
    "cash": "Cash below zero",
    "halal": "Holding no longer halal",
    "summary": "Daily summary",
    "money": "Dividends and fees",
    "problems": "Broker or market problem",
    "runs": "Automation run failed",
}
EVENTS = {"FILL_IMPORTED": "fills", "ORDER_REJECTED": "rejected", "KILL_RULE": "kill", "KILL_ALL": "kill",
          "DIVIDEND": "money", "ACCOUNT_FEE": "money"}  # activity log action: category. Used by: events


def tokens(conn) -> list[str]:
    return (_get(conn, "push:devices") or {}).get("tokens", [])


def disabled(conn) -> list[str]:
    return (_get(conn, "push:off") or {}).get("categories", [])


def send(conn, category: str, title: str, body: str) -> int:
    """Push one notification to every registered phone, unless its category is off. Never raises.
    Tokens Expo reports as DeviceNotRegistered (the app was removed) are dropped.

    Args:
        conn: app DB connection.
        category: a CATEGORIES key, or "test" (always sent).
        title: the bold first line.
        body: the text (shortened to 300 characters).

    Returns:
        How many phones Expo accepted it for.
    """
    to = tokens(conn)
    if not to or category in disabled(conn):
        return 0
    text = " ".join(body.split())
    msgs = [{"to": t, "title": title, "body": text[:297] + "…" if len(text) > 300 else text, "sound": "default",
             "data": {"category": category}} for t in to]
    try:
        resp = requests.post(EXPO_URL, json=msgs, timeout=15)
        resp.raise_for_status()
        tickets = resp.json()["data"]
    except Exception as e:  # Expo unreachable: the alert is lost for the phone (email still has it)
        log.warning("push failed (%s): %s", title, e)
        return 0
    gone = [t for t, r in zip(to, tickets) if (r.get("details") or {}).get("error") == "DeviceNotRegistered"]
    if gone:
        _put(conn, "push:devices", {"tokens": [t for t in tokens(conn) if t not in gone]})
    return sum(r.get("status") == "ok" for r in tickets)


def _message(conn, row) -> tuple[str, str]:
    """Title and body for one activity log entry (an EVENTS action)."""
    d, rel = json.loads(row["details"]), json.loads(row["related"])
    p = conn.execute("SELECT name FROM portfolios WHERE id = ?", (rel.get("portfolioId") or row["entity_id"],)).fetchone()
    pf = p["name"] if p else ""
    match row["action"]:
        case "FILL_IMPORTED":
            qty, price = float(d["quantity"]), float(d["price"])
            return (f"{'Sold' if d['side'] == 'SELL' else 'Bought'} {qty:g} {d['symbol']}",
                    f"{pf} · at ${price:,.2f} (${qty * price:,.2f})")
        case "ORDER_REJECTED":
            return f"{'Sell' if d.get('side') == 'SELL' else 'Buy'} {d.get('symbol', '')} rejected", row["error"] or pf
        case "KILL_RULE":
            return "Kill rule paused buys", f"{pf} is {d['drawdownPercent']}% below its peak."
        case "KILL_ALL":
            return "Kill all", f"{d.get('sells', 0)} sells placed, {d.get('cancelled', 0)} buys cancelled."
        case "DIVIDEND":
            return f"Dividend {d['symbol']}", f"${float(d['amount']):,.2f} {d.get('kind') or ''}"
        case _:
            return "Broker fee", f"${float(d['amount']):,.2f} {d.get('description') or ''}"


def events(conn) -> None:
    """Push the new EVENTS entries of the activity log (since the last call). The first call only sets the cursor,
    so old entries are never pushed.

    Used by: automations.scheduler.AutomationScheduler._loop.
    """
    cursor = (_get(conn, "push:cursor") or {}).get("id")
    last = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0
    if cursor is not None and tokens(conn):
        marks = ",".join("?" * len(EVENTS))
        for row in conn.execute(f"SELECT * FROM audit_log WHERE id > ? AND id <= ? AND action IN ({marks}) ORDER BY id",
                                (cursor, last, *EVENTS)).fetchall():
            try:
                send(conn, EVENTS[row["action"]], *_message(conn, row))
            except Exception:  # a malformed entry must not block the ones after it
                log.exception("push for activity %s failed", row["id"])
    if cursor != last:
        _put(conn, "push:cursor", {"id": last})
