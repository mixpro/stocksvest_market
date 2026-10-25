# TODO

Each item has a permanent ID. IDs are never reused or renumbered; finished items move to **Done** with their ID.
Status: `[ ]` open · `[~]` in progress · `[x]` done · `[-]` dropped.

Longer write-ups for an item can go in `todos/` (e.g. `todos/001-sec-user-agent-for-sectors.md`).

Prefixes: **BT** backtester · **DATA** market data · **STRAT** strategies to test · **FEAT** new filter/automation features · **OPS** production and fixes · **IDEA** parked ideas.

Suggested order: BT-1 → BT-2 → STRAT-1..3 → DATA-1 → FEAT-1 → BT-4 → rest.

---

## BT: Backtester accuracy

- [x] **BT-1: Fill at the next price, not the signal's own close**
  - Today a buy is filled at the same hourly close the filter just saw, so results look better than reality.
  - Fix: fill at the next bar's open (or next hour's close) ± spread.
  - Done 2026-10-25: default `--fill NEXT_VWAP` (next hour's VWAP ± spread); `SAME_CLOSE` kept for comparison.
  - Result on the live setup (2025-10-24..2026-10-23): −85.6% same close vs −86.2% next VWAP. The bias was small.
- [x] **BT-2: Schedule timing in the engine**
  - The engine steps hourly 10:00–15:00 ET every day. Real automations run on their own schedule.
  - Needed for STRAT-1 (weekly at 11:00) and STRAT-2 (daily at 11:00).
  - Option: read each automation's schedule and only run it when it would fire.
  - Done 2026-10-25: default `--steps SCHEDULE` runs each automation once in every hour its schedule fires in (a 09:30 fire runs at 09:00, since bars are hourly). `HOURLY` kept for comparison.
  - Live setup on its real schedules: −90.0% (3,542 buys, win rate 29%) vs −86.2% hourly 10–15. The extra 09:00–10:00 runs hurt.
- [x] **BT-3: True buy-and-hold benchmark**
  - The current benchmark rebalances daily to equal weight, so it's an active strategy itself.
  - Add: buy the universe (or SPY-like proxy) once on day 1, hold to the end.
  - Done 2026-10-25: `summary.buyHold` = whole universe and liquid (> $50M/day) equal-weight buy-and-hold.
  - 2025-10-24..2026-10-23: all +15.6%, > $50M/day +18.7%, > $100M/day +19.7%.
- [ ] **BT-4: Test entries and exits separately**
  - Entries: average forward 1/5/20/60-day return of what each buy rule picks, vs. the universe.
  - Exits: same entries, different sell rules. Also what a stock did 1/5/20 days after we sold it.
  - MAE/MFE per trade (worst and best point while held).
  - Why: "stops lose, take-profits win" is partly an accounting artifact. A stop only closes losers by definition.
- [ ] **BT-5: Robustness checks**
  - 2× spread cost stress.
  - Walk-forward: tune on one period, test on the next.
  - Keep the last 12 months untouched until a strategy is final.
  - Block bootstrap of daily returns for a confidence range.
- [ ] **BT-6: Minute-resolution runs**
  - Minute rules (F.S 3's 3-minute step) can't be tested at hourly steps.
  - Limit: only ~7 days of minute bars are kept. Needs DATA-2 first.

## DATA: Market data

- [ ] **DATA-1: Keep ~10 years of daily bars**
  - Today `DAILY_HISTORY_DAYS=730` (2 years), so a 1-year rule has only 1 year to be tested on.
  - Alpaca's free plan serves daily bars back to 2016. Change the setting, re-sync, check DB size.
  - Caveat: survivorship bias. The universe is today's halal list, so stocks that died or delisted are missing.
- [ ] **DATA-2: Keep more minute bars** (only if BT-6 is wanted)
  - Check DB size impact first.
- [ ] **DATA-3: Buy side is still 16 min delayed** (deferred by you)
  - Option 1: Alpaca paid plan (real-time SIP).
  - Option 2: IEX feed top-up (free, real-time, thinner coverage).
  - Held symbols are already live via IBKR (done).

## STRAT: Strategies to backtest (from Kimi)

Built by `scripts/backtest_strategies.py`. Results 2025-10-24..2026-10-23 (liquid buy-and-hold +18.7%, live setup −90%):

| | Return | Max drawdown | Trades | Win rate | Avg invested |
|---|---|---|---|---|---|
| STRAT-1 | +9.2% | −$2,332 | 116 buys | 37% | $10.1k |
| STRAT-2 | **+23.5%** | −$1,475 | 396 buys | 47% | $8.0k |
| STRAT-3 | +0.6% | −$384 | 318 buys | 47% | $1.4k |

One year of a rising market only: needs DATA-1 (10 years, incl. 2022) before trusting any of it.

Shared base: price > $10, 20-day dollar volume > $50M, max spread 0.30%, buy at 11:00 ET, $500 per position, max 5% per position.

- [x] **STRAT-1: Slow momentum** (Kimi's top pick): +9.2%, trails buy-and-hold
  - Buy, weekly: 1-year > +20%, 6-month > +10%, 3-month > 0, 1-month −5%..+12%, 1-day −3%..+2%.
  - Top 5 by daily change ascending. Re-buy cooldown 30 days.
  - Sell, daily: held > 30 days and (3-month < 0 or 6-month < 0), or unrealized < −15% and 1-month < −8%. No take-profit.
- [x] **STRAT-2: Pullback in an uptrend**: +23.5%, best so far
  - Buy, daily: 1-year > +30%, 6-month > +15%, 1-month > +5%, 5-day −8%..−2%, 1-day −4%..+1%. Top 3 by daily change ascending.
  - Sell: held > 21 days, or 3-month < 0, or unrealized < −12%.
- [x] **STRAT-3: Short-term rebound** (low conviction): +0.6%, mostly in cash
  - Only very liquid: price > $20, > $100M/day, spread ≤ 0.2%.
  - Buy: 1-day −5%..−2%, 5-day −12%..−4%. Half-size positions.
  - Sell: held > 7 days or unrealized < −10%.
- [ ] **STRAT-4: Decide what to do with the live setup**
  - Backtest verdict so far: live setup −71%, spike-chasing rules removed −9%, benchmark +20%.
  - After STRAT-1..3: replace, keep paper-testing, or turn off.
  - Cleanup: R.B E is redundant in the Buy group union.

## FEAT: New features (ranked by value for effort)

- [ ] **FEAT-1: Momentum ranking key (12-1 and 6-1)**
  - Return over the last 12 (or 6) months, skipping the latest month. Strongest known selection signal.
  - Today `rankBy` only has daily change, price, volume.
- [ ] **FEAT-2: Volatility (ATR)**
  - Filter step on ATR %, position sizing by volatility, ATR-based stops.
- [ ] **FEAT-3: Portfolio limits**
  - Max open positions, max per sector, max buys/sells per day (turnover cap).
- [ ] **FEAT-4: Trend position**
  - 200-day moving average (price above/below), distance from 52-week high.
- [ ] **FEAT-5: Sector-relative strength**
  - Stock's change vs. its sector's average over the same period.
- [ ] **FEAT-6: Earnings calendar**
  - Skip buys N days before earnings; needs a data source.
- [ ] **FEAT-7: Market regime**
  - Only buy when the broad market is above its 200-day average (or similar).
- [ ] **FEAT-8: Quality fundamentals**
  - Profitability, debt. Needs a data source.
- [ ] **FEAT-9: Correlation clusters** (the graph-theory idea)
  - Group stocks that move together; hold at most N per cluster.
  - Kimi ranked this low for returns, useful for diversification.

## OPS: Production and fixes

- [ ] **OPS-1: Check live bars Monday 2026-10-26 after 09:30 ET**
  - `GET /v1/live` → `lastBarAt` should update every few seconds for held symbols.
- [ ] **OPS-2: False cash-mismatch alerts**
  - Sync compares app vs. broker while fills are only partly imported.
  - Fix: skip the cash/position comparison while the broker reports fills not yet imported.
- [ ] **OPS-3: `claude-lite` defaults to DeepSeek**
  - Add `export ANTHROPIC_MODEL="my-kimi-k3.1-pro-highspeed"` to `~/.local/bin/claude-lite`, or keep using `--model sonnet`.
- [ ] **OPS-4: Ponytail statusline badge** (optional)

## IDEA: Parked

- [ ] **IDEA-1: Researcher / advisor**
  - An assistant that reviews filters, positions and results and suggests changes. You said leave it for now.

---

## Done

- [x] todos/001: SEC contact so sectors sync (2026-10-17)
- [x] Stop-loss on buy price (UNREALIZED_PNL step)
- [x] Spread guard (`maxSpreadPercent`), re-buy cooldown (`rebuyCooldownMinutes`)
- [x] Average volume filter step (AVERAGE_VOLUME)
- [x] Require live price guard + editor checkbox
- [x] 1-minute schedules
- [x] Real-time IBKR bars for held symbols
- [x] Backtester on real history
- [x] Holding period filter step (HOLDING_PERIOD)
- [x] Kimi review of the strategy
