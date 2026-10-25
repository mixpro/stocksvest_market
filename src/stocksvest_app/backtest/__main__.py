"""Backtest from the command line, on COPIES of the databases (e.g. the nightly backup), never the live files.

    python -m stocksvest_app.backtest --app-db backup/app.db --market-db backup/market.db \
        --buy aut_... --sell aut_... --start 2026-04-01 --end 2026-10-23 --out result.json \
        [--overrides '{"buy": {"orderGuards": {"maxSpreadPercent": 0.5}}}']
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .engine import FILL_MODES, STEP_MODES, run
from .history import HistoricalMarket


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m stocksvest_app.backtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app-db", type=Path, required=True, help="copy of app.db (automations, filters, budget)")
    p.add_argument("--market-db", type=Path, required=True, help="copy of market.db (bars, calendar)")
    p.add_argument("--buy", required=True, help="Buy automation id")
    p.add_argument("--sell", required=True, help="Sell automation id")
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--budget", type=float, default=10_000)
    p.add_argument("--overrides", type=json.loads, default=None, help="JSON merged into the definitions")
    p.add_argument("--fill", choices=FILL_MODES, default="NEXT_VWAP",
                   help="NEXT_VWAP: next hour's VWAP at ask/bid (default); NEXT_VWAP_STRICT: same, but the next day's VWAP "
                        "where only daily bars exist; SAME_CLOSE: the signal's own price")
    p.add_argument("--steps", choices=STEP_MODES, default="SCHEDULE",
                   help="SCHEDULE: each automation at its own fire times (default); HOURLY: both every hour 10-15")
    p.add_argument("--spread-factor", type=float, default=1.0, help="cost stress: modelled spreads x this")
    p.add_argument("--out", type=Path, help="write the full result (equity per day, every trade) as JSON")
    a = p.parse_args(argv)
    hist = HistoricalMarket(a.market_db, a.start, a.end)
    hist.spread_factor = a.spread_factor
    result = run(a.app_db, a.market_db, a.buy, a.sell, a.start, a.end, a.budget, a.overrides, hist=hist,
                 progress=lambda day, eq: print(f"{day}  equity ${eq:,.2f}", file=sys.stderr), fill=a.fill,
                 steps=a.steps)
    if a.out:
        a.out.write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps(result["summary"], indent=1))


if __name__ == "__main__":
    main()
