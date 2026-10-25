"""Compare 10y-*.json runs: total, per year, max drawdown (dates), worst year, 2021 and 2026. Args: dir name,..."""

import json
import sys
from pathlib import Path

B = Path(sys.argv[1])
print(f"{'run':26} {'total':>7} {'/yr':>6} {'max dd':>7}  {'dd from → to':23} {'worst yr':>9} {'2021':>7} {'2026':>7}")
def curve(n):
    """Equity per day of a run, or of several runs added together (names joined by +: separate budgets)."""
    runs = [json.load(open(B / f"10y-{x}.json")) for x in n.split("+")]
    eq = [{x["date"]: x["equity"] for x in r["equity"]} for r in runs]
    days = sorted(set.intersection(*(set(q) for q in eq)))
    return [{"date": d, "equity": sum(q[d] for q in eq)} for d in days], sum(r["params"]["budget"] for r in runs)


for n in sys.argv[2].split(","):
    e, start = curve(n)
    pk, dd, span, pkd = 0, 0, ("", ""), ""
    years = {}
    for x in e:
        if x["equity"] > pk:
            pk, pkd = x["equity"], x["date"]
        if x["equity"] / pk - 1 < dd:
            dd, span = x["equity"] / pk - 1, (pkd, x["date"])
        years[x["date"][:4]] = x["equity"]
    yr, last = {}, start
    for y, v in years.items():
        yr[y], last = (v / last - 1) * 100, v
    total = e[-1]["equity"] / start
    worst = min(yr, key=yr.get)
    print(f"{n:26} {(total - 1) * 100:+6.0f}% {(total ** (1 / 8.98) - 1) * 100:5.1f}% {dd * 100:6.0f}%  "
          f"{span[0]} → {span[1]}  {worst} {yr[worst]:+4.0f}% {yr['2021']:+6.1f}% {yr['2026']:+6.1f}%")
