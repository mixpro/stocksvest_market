#!/usr/bin/env python3
"""IDEA-3 phase-in: 5B moves from one Monday budget to five weekday budgets without forced selling.

Each weekday before the 11:00 ET buys (cron), the Monday budget's free cash above its fair share (a fifth of the
portfolio) is parked in the portfolio's unallocated cash. When the parked cash covers a weekday budget's whole
missing share, it goes there in one piece, so a budget fills at its full 5% size instead of with many tiny positions.
Does nothing once the five are balanced. Equity here is cash + cost of holdings (close enough for a split).

Usage: tranche-phase-in.py  (reads APP_API_KEYS from the .env next to it)
"""
import json
import pathlib
import urllib.request
from decimal import ROUND_DOWN, Decimal

BASE = "http://100.126.3.52:8100/v1"
PORTFOLIO = "pf_059512a438f34e0ca2439864f7f87d5b"  # Momentum 5B
SOURCE = "bud_11366221bbc3485b9ca9da8e4776e753"  # the Monday budget
MIN_MOVE = Decimal(50)  # ponytail: skip dust moves
ENV = pathlib.Path(__file__).with_name(".env").read_text()
KEY = next(line.split("=", 1)[1].split(",")[0].strip() for line in ENV.splitlines() if line.startswith("APP_API_KEYS="))


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def cents(x):
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


budgets = call("GET", f"/portfolios/{PORTFOLIO}/budgets")["budgets"]
equity = {b["id"]: Decimal(str(b["cash"])) + Decimal(str(b["invested"])) for b in budgets}
parked = Decimal(str(call("GET", f"/portfolios/{PORTFOLIO}")["unallocatedCash"]))
target = (sum(equity.values()) + parked) / len(budgets)
src = next(b for b in budgets if b["id"] == SOURCE)

spare = cents(min(Decimal(str(src["available"])), equity[SOURCE] - target))
if spare >= MIN_MOVE:
    call("POST", f"/budgets/{SOURCE}/withdraw", {"amount": str(spare), "note": "IDEA-3 phase-in: park"})
    parked += spare
    print(f"parked {spare} from the Monday budget")

for b in sorted((b for b in budgets if b["id"] != SOURCE), key=lambda b: equity[b["id"]]):
    need = cents(target - equity[b["id"]])
    if need >= MIN_MOVE and parked >= need:
        call("POST", f"/budgets/{b['id']}/allocate", {"amount": str(need), "note": "IDEA-3 phase-in: fill"})
        parked -= need
        print(f"moved {need} to {b['name']}")
print(f"target {target:.2f}, parked {parked:.2f}, " + ", ".join(f"{b['name']} {equity[b['id']]:.0f}" for b in budgets))
