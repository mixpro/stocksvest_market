"""Is leaving some stocks out of a strategy better than luck? Runs the strategy as is, without the candidate tickers,
and without N random sets of the same size (placebos), then ranks the candidate among the placebos.

A placebo leaves out as many stocks the baseline actually traded as the candidate does (drawn from the other traded
stocks), so both change the same number of trades. Leaving out any traded stocks moves the result a lot (~90 points
over 9 years in WATCH-12) through which later buys the freed cash goes to: only a candidate above (nearly) every
placebo means something.

    PYTHONPATH=src .venv/bin/python scripts/backtest_placebo.py --app-db app.db --market-db market.db \\
        --buy aut_... --sell aut_... --start 2017-11-01 --end 2026-10-30 --exclude biotech.txt \\
        [--placebos 10] [--seed 1] [--jobs 6] [--out dir]
"""

import argparse
import json
import random
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

from stocksvest_app.backtest import engine
from stocksvest_app.backtest.history import HistoricalMarket

A = None  # the parsed arguments, in each worker
HIST = None  # one loaded HistoricalMarket per worker, reused across its runs


def _init(args):
    global A, HIST
    A, HIST = args, HistoricalMarket(args.market_db, args.start, args.end)


def _run(job: tuple[str, list[str]]) -> tuple[str, list[str], dict]:
    """One run without these tickers: (name, tickers, result)."""
    name, tickers = job
    HIST.exclude = set(tickers)
    r = engine.run(A.app_db, A.market_db, A.buy, A.sell, A.start, A.end, A.budget, hist=HIST)
    if A.out:
        (A.out / f"{name}.json").write_text(json.dumps(r, default=str))
    return name, tickers, r


def traded(r: dict) -> set[str]:
    return {t["symbol"] for t in r["trades"] if t["side"] == "BUY"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app-db", type=Path, required=True)
    p.add_argument("--market-db", type=Path, required=True)
    p.add_argument("--buy", required=True)
    p.add_argument("--sell", required=True)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--budget", type=float, default=10_000)
    p.add_argument("--exclude", type=Path, required=True, help="the candidate: file of tickers to leave out")
    p.add_argument("--placebos", type=int, default=10)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--jobs", type=int, default=6, help="parallel runs (each loads the market copy: ~3 GB for 9y)")
    p.add_argument("--out", type=Path, help="directory for each run's full result")
    a = p.parse_args(argv)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
    candidate = sorted(set(a.exclude.read_text().replace(",", " ").split()))

    with ProcessPoolExecutor(a.jobs, initializer=_init, initargs=(a,)) as pool:
        _, _, base = pool.submit(_run, ("baseline", [])).result()
        bought = traded(base)
        hit = sorted(bought & set(candidate))
        print(f"baseline {base['summary']['returnPct']:+.0f}%, {len(bought)} stocks bought; "
              f"the candidate leaves out {len(candidate)} tickers, {len(hit)} of them bought: {' '.join(hit)}")
        if not hit:
            return print("the candidate changes no trade: nothing to test")
        rng = random.Random(a.seed)
        others = sorted(bought - set(candidate))
        jobs = [("candidate", candidate)] + [(f"placebo-{n}", rng.sample(others, len(hit)))
                                             for n in range(1, a.placebos + 1)]
        runs = list(pool.map(_run, jobs))

    print(f"\n{'run':12} {'return':>8} {'max dd $':>10} {'buys':>5} {'win':>5}  left out (bought ones)")
    for n, tickers, r in sorted([("baseline", [], base)] + runs, key=lambda x: -x[2]["summary"]["returnPct"]):
        s = r["summary"]
        print(f"{n:12} {s['returnPct']:+7.0f}% {s['maxDrawdownUsd']:10,.0f} {s['buys']:5} {s['winRate'] or 0:5.2f}  "
              f"{' '.join(sorted(set(tickers) & bought))[:60]}")
    cand = runs[0][2]["summary"]["returnPct"]
    plac = sorted(r["summary"]["returnPct"] for n, _, r in runs[1:])
    beaten = sum(x < cand for x in plac)
    print(f"\ncandidate {cand:+.0f}% beats {beaten} of {len(plac)} placebos "
          f"(placebos {plac[0]:+.0f}% .. {plac[-1]:+.0f}%, median {plac[len(plac) // 2]:+.0f}%); "
          f"by luck alone it would beat all {len(plac)} with odds 1 in {len(plac) + 1}")


if __name__ == "__main__":
    main()
