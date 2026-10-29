# Live trading rules

Written 2026-10-29, before any real money beyond the $50 test. Change this file only on purpose, with a dated
commit that says why. **No overriding these rules during a drawdown.**

## Accounts

- Broker: Alpaca, margin-type account locked to **1× (no borrowing)**, `no_shorting` on, no options, 2FA. No
  High-Yield Cash, no securities lending. (Scholar: a margin account is fine as long as nothing is borrowed.)
- Live keys (`ALPACA_LIVE_KEY` / `_SECRET`) and `ALLOW_LIVE=1` exist only in tinkergw's `.env`.
- Keep about **1% of the account unallocated** so fees and slippage never push cash below zero (the app emails
  "Negative cash" if they do).

## Strategies and money

- No ETFs for now. One portfolio per strategy on the LIVE account:
  - **5B**: five weekday budgets (IDEA-3 tranches), top 20 by 12-1 momentum, 5% of a budget each, fractional shares;
    sold when held over 30 days and down over 6 months.
  - **Core 40**: the 40 most traded halal stocks, rebalanced monthly.
- Settings are frozen as they are on paper on 2026-10-29. Any change is tested on paper first, then written here.

## Scaling

| Step | Amount | Go to the next step only if, over the period |
|---|---|---|
| 1 | $10k (5B $5k, Core 40 $5k) | 6–8 weeks: zero unexplained broker mismatches; fills within the backtest's cost assumptions; no manual fixes; kill rule not tripped |
| 2 | $25k | the same, 2–3 months |
| 3 | $50k | the same; **stop around $55k** until US estate tax (non-US person, ~$60k) is settled with the accountant |

## Kill rule

- **Automatic (the app):** a portfolio **30% below its peak** (equity ÷ allocated, after-close snapshots) gets its buy
  automations paused and an email is sent. Sells keep running; nothing is sold by the rule. Setting:
  `KILL_DRAWDOWN_PERCENT` in `.env` (`automations/kill_rule.py`).
- **Before resuming:** wait at least 4 weeks after the trip, compare with paper and the backtest's worst drops (5B:
  −38%, −45% with delisted stocks), and write the decision here.
- **Monthly review (by hand):** if after 12 months live a strategy trails the halal universe's median 12-month
  return by more than 10 points, stop adding money to it and decide here whether to keep it.
- **Emergency:** the **Kill all** button (header) switches automations off, cancels buys and sells everything.

## Halal

- Holdings that the halal list marks NON_COMPLIANT are sold at market in the next regular session (automatic).
  Other removals (pending, error, delisted) are emailed daily and decided by hand.
- Purification: from the yearly dividend report (`/v1/portfolios/{id}/dividends`); method per your scholar.
- Halal list refresh: upload a new halal DB at least quarterly.

## Tax (Israel resident)

- W-8BEN at Alpaca: US withholds 25% of dividends (treaty), credited against Israeli tax.
- Capital gains are self-reported: download the yearly **sales CSV** (portfolio page, "Tax records") and the
  dividend report for the accountant. Open questions in TODO LIVE-11.
