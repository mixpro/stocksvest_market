"""Daily check of the IB Gateway's support end date (run by cron on tinkergw, deploy/tinkergw/gateway-check.sh).

IBKR announces when a gateway version stops being supported in API message 2172, sent when a program connects:
  "The version of the application you are running, 1045.1, needs to be upgraded, as it will be desupported on
   20261215. The minimum supported version at that time will be 1050.1."
This connects read-only (own client id), reads that message, and emails when the date is within WARN_DAYS:
first at 21 days, then reminders at 7 and 2 days, each once. The email names IBKR's current stable/latest
versions and the upgrade steps.

  python -m stocksvest_market.gateway_check [--installed 1050] [--dry-run] [--test-email]
"""

import argparse
import asyncio
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

WARN_DAYS = (21, 7, 2)        # email when this many days or fewer are left, once each. Used by: due_milestone
CLIENT_ID = 33                # this check's own API client id (bridge 17, market service 31). Used by: read_warning
STATE = Path(os.environ.get("DATA_DIR", "/data")) / "gateway-check.json"  # what was already emailed. Used by: main
# IBKR's version files next to its installers. Used by: ibkr_versions
VERSION_URL = "https://download2.interactivebrokers.com/installers/ibgateway/{channel}-standalone/version.json"
WARNING = re.compile(
    r"running,\s*(?P<version>\d+(?:\.\d+)*),\s*needs to be upgraded.*?desupported on\s*(?P<date>\d{8})"
    r"(?:.*?minimum supported version at that time will be\s*(?P<minimum>\d+(?:\.\d+)*))?", re.S)  # Used by: parse_warning


def parse_warning(message: str) -> dict | None:
    """Read IBKR's desupport warning.

    Args:
        message: text of API message 2172.

    Returns:
        {"version", "date" (date), "minimum"}, or None if it isn't a desupport warning.

    Used by: read_warning, tests.
    """
    m = WARNING.search(message)
    if not m:
        return None
    return {"version": m["version"], "date": datetime.strptime(m["date"], "%Y%m%d").date(),
            "minimum": m["minimum"]}


def due_milestone(days_left: int) -> int | None:
    """The reminder that is due: the smallest of WARN_DAYS that days_left has reached, or None (too early).

    Args:
        days_left: days until the support end date (0 or less = already unsupported).

    Used by: decide, tests.
    """
    due = [m for m in WARN_DAYS if days_left <= m]
    return min(due) if due else None


def decide(warning: dict | None, today: date, state: dict) -> tuple[str | None, dict]:
    """Whether to email now.

    Args:
        warning: parse_warning result, or None (no warning: the installed version is supported).
        today: today's date.
        state: what was already emailed ({"sent": "<date>:<milestone>"}).

    Returns:
        (the reminder key to email, e.g. "2026-12-15:21", or None; the new state).

    Used by: main, tests.
    """
    if warning is None:
        return None, {}
    milestone = due_milestone((warning["date"] - today).days)
    if milestone is None:
        return None, state
    key = f"{warning['date'].isoformat()}:{milestone}"
    if state.get("sent") == key or (state.get("sent", "").startswith(warning["date"].isoformat() + ":")
                                    and int(state["sent"].split(":")[1]) <= milestone):
        return None, state
    return key, {"sent": key}


async def read_warning(host: str, port: int, wait: float = 3.0) -> tuple[dict | None, list[str]]:
    """Connect read-only to the gateway, collect its messages for a few seconds, disconnect.

    Args:
        host: gateway API host.
        port: gateway API port.
        wait: seconds to listen after connecting.

    Returns:
        (the desupport warning or None, all message texts seen).

    Raises:
        Exception: the gateway can't be reached.

    Used by: main.
    """
    from ib_async import IB

    ib = IB()
    seen: list[str] = []
    ib.errorEvent += lambda req_id, code, msg, contract: seen.append(f"{code}: {msg}")
    await ib.connectAsync(host, port, clientId=CLIENT_ID, readonly=True, timeout=20)
    try:
        await asyncio.sleep(wait)
    finally:
        ib.disconnect()
    for text in seen:
        if text.startswith("2172:") and (w := parse_warning(text)):
            return w, seen
    return None, seen


def ibkr_versions() -> dict:
    """IBKR's current stable and latest gateway versions, from the version files next to its installers.

    Returns:
        {"stable": "10.50.1f (20261005 16:18:53)", "latest": ...}; a channel is "unknown" if unreadable.

    Used by: email_text.
    """
    out = {}
    for channel in ("stable", "latest"):
        try:
            with urllib.request.urlopen(VERSION_URL.format(channel=channel), timeout=20) as r:
                body = r.read().decode()
            data = json.loads(body[body.index("{"):body.rindex("}") + 1])
            out[channel] = f"{data['buildVersion']} ({data['buildDateTime']})"
        except Exception:
            out[channel] = "unknown"
    return out


def email_text(warning: dict, installed: str | None, today: date) -> tuple[str, str]:
    """Subject and body of the reminder.

    Args:
        warning: parse_warning result.
        installed: the installed major version from gatewaystart.sh (e.g. "1050"), if known.
        today: today's date.

    Used by: main.
    """
    days = (warning["date"] - today).days
    versions = ibkr_versions()
    subject = (f"IB Gateway {warning['version']} stops being supported in {days} days ({warning['date']})"
               if days > 0 else f"IB Gateway {warning['version']} is no longer supported (since {warning['date']})")
    body = f"""IBKR says the IB Gateway on tinkergw ({warning['version']}, installed folder {installed or '?'}) will be
desupported on {warning['date']} ({days} days from now). The minimum supported version then: {warning['minimum'] or '?'}.

Available from IBKR now:
  stable  {versions['stable']}
  latest  {versions['latest']}

Upgrade (deploy/README.md, "Upgrading the IB Gateway"), no sudo needed, about 5 minutes when markets are closed:
  1. Download the stable Linux installer to tinkergw (ibgateway-stable-standalone-linux-x64.sh).
  2. Back up:  tar -czf ~/gateway-backup-$(date +%F).tar.gz -C ~ Jts ibc
  3. Install next to the current one:  sh ./<installer>.sh -q -dir ~/Jts/ibgateway/<NNNN>
  4. In ~/ibc/gatewaystart.sh set TWS_MAJOR_VRSN=<NNNN> (keep a copy), then restart the gateway.
  5. Check: bridge ibConnected, market service liveQuotes connected; roll back by restoring the copy.

You'll get reminders at 7 and 2 days if it is still the same version.
"""
    return subject, body


def send(subject: str, text: str) -> None:
    """Email ALERT_EMAIL_TO through the SMTP relay (SMTP_* from the environment, Brevo, STARTTLS).

    Args:
        subject: subject line (the "[StocksVest]" prefix is added).
        text: plain-text body.

    Raises:
        KeyError: SMTP settings missing. smtplib.SMTPException / OSError: the relay failed.

    Used by: main.
    """
    e = os.environ
    msg = EmailMessage()
    msg["From"] = e.get("ALERT_EMAIL_FROM", "StocksVest <alerts@stocksvest.net>")
    msg["To"] = e["ALERT_EMAIL_TO"]
    msg["Subject"] = f"[StocksVest] {subject}"
    msg.set_content(text)
    with smtplib.SMTP(e["SMTP_HOST"], int(e.get("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(e["SMTP_USERNAME"], e["SMTP_PASSWORD"])
        s.send_message(msg)


def main(argv: list[str] | None = None) -> int:
    """Run one check: read IBKR's warning, email the due reminder, remember it.

    Args:
        argv: command line arguments (default sys.argv[1:]).

    Returns:
        Exit code: 0 ok, 1 the gateway couldn't be reached.
    """
    p = argparse.ArgumentParser(prog="gateway_check")
    p.add_argument("--installed", default=os.environ.get("GATEWAY_VERSION"), help="installed major version, e.g. 1050")
    p.add_argument("--dry-run", action="store_true", help="report only: no email, state unchanged")
    p.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = p.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.test_email:
        send("Gateway check test", f"The IB Gateway check on tinkergw can send email (installed: {args.installed}).")
        print(f"{now:%F %T} test email sent")
        return 0

    host, port = os.environ.get("IBKR_HOST", "127.0.0.1"), int(os.environ.get("IBKR_PORT", "4002"))
    try:
        warning, _ = asyncio.run(read_warning(host, port))
    except Exception as e:  # the gateway is down: the app's alerts report that during market hours
        print(f"{now:%F %T} gateway not reachable at {host}:{port}: {e or type(e).__name__}")
        return 1

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    key, new_state = decide(warning, now.date(), state)
    if warning is None:
        print(f"{now:%F %T} installed {args.installed}: IBKR reports no support end date")
    else:
        print(f"{now:%F %T} installed {args.installed}: {warning['version']} desupported on {warning['date']} "
              f"({(warning['date'] - now.date()).days} days), minimum then {warning['minimum']}; "
              f"{'emailing ' + key if key else 'no email due'}")
    if args.dry_run:
        return 0
    if key:
        subject, body = email_text(warning, args.installed, now.date())
        send(subject, body)
    STATE.write_text(json.dumps(new_state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
