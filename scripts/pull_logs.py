#!/usr/bin/env python3
"""Pull the app's activity log (GET /v1/activity) to the terminal or a file.

Standard library only. Reads APP_URL and APP_KEY from the environment, or the first APP_API_KEYS value from .env.

Examples:
    python3 scripts/pull_logs.py                                   # last 200 entries, as a table
    python3 scripts/pull_logs.py --since 2h --outcome FAILED       # failures in the last 2 hours
    python3 scripts/pull_logs.py --id aut_123... --all             # everything about (or mentioning) an id
    python3 scripts/pull_logs.py --correlation req_abc...          # everything one request / run caused
    python3 scripts/pull_logs.py --since 1d --format csv --out logs.csv
    python3 scripts/pull_logs.py --follow --source SYNC            # keep printing new entries (Ctrl+C to stop)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_URL = "http://localhost:8100"
PAGE = 1000
FIELDS = ["id", "at", "source", "actor", "action", "entityType", "entityId", "outcome", "status", "error",
          "method", "path", "durationMs", "correlationId", "related", "details"]


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def parse_time(value: str | None) -> str | None:
    """'30m', '2h', '3d', '1w' (ago), or an ISO date/time -> UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)([mhdw])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        t = datetime.now(timezone.utc) - delta
    else:
        try:
            t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            sys.exit(f"error: can't read the time '{value}': use 30m, 2h, 3d, 1w or an ISO date like 2026-10-19T09:30")
        if t.tzinfo is None:
            t = t.astimezone()  # local time
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def activity(self, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(f"{self.url}/v1/activity?{query}", headers={"X-API-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            sys.exit(f"error: {e.code} from {self.url}: {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"error: cannot reach {self.url}: {e.reason}")


def fetch(client: Client, filters: dict, limit: int | None) -> list[dict]:
    """Newest first, following pages until `limit` entries (None = all)."""
    out, before = [], None
    while True:
        want = PAGE if limit is None else min(PAGE, limit - len(out))
        if want <= 0:
            return out
        page = client.activity(**filters, limit=want, beforeId=before)
        out += page["entries"]
        before = page.get("nextBeforeId")
        if not before:
            return out


def short(value, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(entries: list[dict], file) -> None:
    for e in entries:
        local = datetime.fromisoformat(e["at"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        entity = f"{(e['entityType'] or '').lower()} {e['entityId'] or ''}".strip()
        result = f"{e['outcome']}{f' {e['status']}' if e['status'] else ''}"
        line = (f"{local}  {short(e['source'], 9):9}  {short(e['action'], 30):30}  {short(entity, 44):44}  "
                f"{result:10}  {short(e['error'], 70)}")
        print(line.rstrip(), file=file)


def write(entries: list[dict], fmt: str, file) -> None:
    if fmt == "table":
        print_table(entries, file)
    elif fmt == "json":
        json.dump(entries, file, indent=2)
        file.write("\n")
    elif fmt == "jsonl":
        for e in entries:
            file.write(json.dumps(e) + "\n")
    else:  # csv
        w = csv.DictWriter(file, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for e in entries:
            w.writerow({**e, "related": json.dumps(e["related"]), "details": json.dumps(e["details"])})


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    env = {**load_env(root / ".env"), **os.environ}
    p = argparse.ArgumentParser(description="Pull the StocksVest app's activity log.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Examples:")[1])
    p.add_argument("--url", default=env.get("APP_URL", DEFAULT_URL), help=f"app URL (default {DEFAULT_URL})")
    p.add_argument("--key", default=env.get("APP_KEY") or env.get("APP_API_KEYS", "").split(",")[0],
                   help="app API key (default APP_KEY, or the first APP_API_KEYS value from .env)")
    p.add_argument("--since", help="start: 30m, 2h, 3d, 1w ago, or an ISO date/time")
    p.add_argument("--until", help="end: same formats as --since")
    p.add_argument("--action", help="e.g. AUTOMATION_RUN, ORDER_PLACED, FILTER_DELETE")
    p.add_argument("--source", choices=["API", "SCHEDULER", "SYNC", "SYSTEM"])
    p.add_argument("--outcome", choices=["OK", "FAILED"])
    p.add_argument("--entity-type", help="FILTER, FILTER_GROUP, AUTOMATION, PORTFOLIO, BROKER_ACCOUNT, ORDER, …")
    p.add_argument("--id", help="entries about this id or mentioning it")
    p.add_argument("--correlation", help="everything caused by one request or scheduled run")
    p.add_argument("--limit", type=int, default=200, help="how many entries (default 200)")
    p.add_argument("--all", action="store_true", help="every matching entry (ignores --limit)")
    p.add_argument("--format", choices=["table", "json", "jsonl", "csv"], default="table")
    p.add_argument("--out", type=Path, help="write to this file instead of the terminal")
    p.add_argument("--oldest-first", action="store_true", help="chronological order (default: newest first)")
    p.add_argument("--follow", action="store_true", help="keep printing new entries, oldest first (Ctrl+C to stop)")
    p.add_argument("--interval", type=float, default=5, help="seconds between checks with --follow (default 5)")
    args = p.parse_args()

    if not args.key:
        sys.exit("error: no API key: pass --key, set APP_KEY, or put APP_API_KEYS in .env")
    client = Client(args.url, args.key)
    filters = {"since": parse_time(args.since), "until": parse_time(args.until), "action": args.action,
               "source": args.source, "outcome": args.outcome, "entityType": args.entity_type, "entityId": args.id,
               "correlationId": args.correlation}

    if args.follow:
        if args.format in ("json", "csv"):
            sys.exit("error: --follow prints table or jsonl")
        out = args.out.open("a") if args.out else sys.stdout
        recent = fetch(client, filters, min(args.limit, 50))
        write(list(reversed(recent)), args.format, out)
        last = recent[0]["id"] if recent else 0
        try:
            while True:
                time.sleep(args.interval)
                new = [e for e in fetch(client, filters, 500) if e["id"] > last]
                if new:
                    write(list(reversed(new)), args.format, out)
                    out.flush()
                    last = new[0]["id"]
        except KeyboardInterrupt:
            return

    entries = fetch(client, filters, None if args.all else args.limit)
    if args.oldest_first:
        entries.reverse()
    if args.out:
        with args.out.open("w", newline="") as f:
            write(entries, args.format, f)
        print(f"{len(entries)} entries written to {args.out}", file=sys.stderr)
    else:
        write(entries, args.format, sys.stdout)
        sys.stdout.flush()
        if args.format == "table":
            print(f"({len(entries)} entries)", file=sys.stderr)


if __name__ == "__main__":
    main()
