#!/usr/bin/env python3
"""The VPS's outside view of tinkergw (cron every 5 minutes). Emails through Brevo with the edge's SMTP credentials:
  - the app on tinkergw doesn't answer for DOWN_AFTER (and again when it is back)
  - last night's backup hasn't arrived by BACKUP_DUE (UTC), once per day

  watch.py [--test]      # --test: send one test email and exit
"""

import json
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = Path("/opt/stocksvest-market")
APP_HEALTH = "http://100.126.3.52:8100/health"   # the app on tinkergw, over Tailscale. Used by: main
DOWN_AFTER = timedelta(minutes=15)               # Used by: main
BACKUP_DUE = 5                                    # hour (UTC) by which today's backup must be here. Used by: main
STATE = BASE / "watch-state.json"                 # Used by: main


def env() -> dict:
    """KEY=VALUE pairs of the edge's .env (SMTP username, ALERT_EMAIL_TO).

    Used by: send.
    """
    out = {}
    for line in (BASE / "edge/.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def send(subject: str, text: str) -> None:
    """Email ALERT_EMAIL_TO through Brevo (STARTTLS 587), with the SMTP key from edge/secrets/smtp_password.

    Args:
        subject: subject line (the "[StocksVest]" prefix is added).
        text: plain-text body.

    Used by: main.
    """
    e = env()
    msg = EmailMessage()
    msg["From"] = "StocksVest <alerts@stocksvest.net>"
    msg["To"] = e["ALERT_EMAIL_TO"]
    msg["Subject"] = f"[StocksVest] {subject}"
    msg.set_content(text)
    with smtplib.SMTP("smtp-relay.brevo.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(e["AUTHELIA_NOTIFIER_SMTP_USERNAME"], (BASE / "edge/secrets/smtp_password").read_text().strip())
        s.send_message(msg)


def app_up() -> bool:
    """Whether the app on tinkergw answers its health check within 10 s.

    Used by: main.
    """
    try:
        with urllib.request.urlopen(APP_HEALTH, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> None:
    """One watch pass: check, compare with the saved state, email what changed."""
    if "--test" in sys.argv:
        send("Watcher test", "The VPS watcher can send email.")
        print("sent")
        return
    now = datetime.now(timezone.utc)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    if app_up():
        if state.get("down_mailed"):
            since = datetime.fromisoformat(state["down_since"])
            send("Resolved: tinkergw is reachable again",
                 f"The app on tinkergw answers again (it was unreachable for about "
                 f"{int((now - since).total_seconds() // 60)} min, since {state['down_since']}).")
        state.pop("down_since", None)
        state.pop("down_mailed", None)
    else:
        state.setdefault("down_since", now.isoformat(timespec="seconds"))
        if not state.get("down_mailed") and now - datetime.fromisoformat(state["down_since"]) >= DOWN_AFTER:
            send("Problem: tinkergw is unreachable",
                 f"The app on tinkergw ({APP_HEALTH}) hasn't answered since {state['down_since']}.\n\n"
                 "Possible causes: power or internet at home, Tailscale, or the app container. While it is down, "
                 "no automation runs (nothing trades).\nYou'll get another email when it's back.")
            state["down_mailed"] = True

    today = now.date().isoformat()
    if now.hour >= BACKUP_DUE and state.get("backup_checked") != today:
        if not (BASE / f"backups/app/{today}.db").exists():
            send("Problem: last night's backup is missing",
                 f"No backups/app/{today}.db on the VPS by {BACKUP_DUE:02d}:00 UTC.\n\n"
                 "On tinkergw: ~/stocksvest/backups/backup.log shows what happened.")
        state["backup_checked"] = today

    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
