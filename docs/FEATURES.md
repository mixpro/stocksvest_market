# Features: where to find them and how they work

Every feature concept from the strategy work (TODO.md IDs in brackets): what you see in the app, and the code
behind it. Items marked **not used** are built and tested but not part of the live strategies.

## 1. Filter steps

Filters page → filter editor → "add step". Models in `src/stocksvest_app/filters/models.py`, logic in
`src/stocksvest_app/filters/engine.py` (`apply`), editor metadata in `filters/routes.py`, defaults in `static/app.js`.

| Concept | What you see | How it works |
|---|---|---|
| Stop-loss on buy price (the KELYB fix) | **"Unrealized gain/loss"**: portfolio(s), % or $, less / more / between | `UNREALIZED_PNL`. It compares each held symbol's current value with what it cost, using the live last trade, or the newest minute bar if there's no quote. Example: F.S 3 "< −3%" |
| Volume filter (keep thin stocks out) | **"Average daily trading"**: days (1–260), shares or dollars | `AVERAGE_VOLUME`. It averages completed trading days from the market service's `/v1/volume`; days without trades count as 0. 5B and Core 40 use "20 days, dollars, > $50M" |
| Holding period | **"Holding period"**: portfolio(s), minutes … years | `HOLDING_PERIOD`. It measures time since the oldest open lot of the symbol in that portfolio. 5B Sell uses "> 30 days" |
| Exclude symbols | **"Exclude symbols"**: a comma-separated ticker list | `EXCLUDE_SYMBOLS`. Drops the listed tickers. Core 40's buy filter excludes GOOG so Alphabet is bought once (GOOGL) |
| Indicators [FEAT-2/4/6/8] | **"Indicator (trend, volatility, 52-week high)"**: pick one, compare | `INDICATOR`. From market `GET /v1/indicators`: `VOLATILITY_20`, `ATR_14_PERCENT`, `PRICE_VS_SMA_50/200`, `FROM_52W_HIGH` (completed daily bars); `ROE`, `NET_MARGIN`, `DEBT_TO_ASSETS`, `DAYS_SINCE_EARNINGS`, `DAYS_TO_EARNINGS` (SEC filings, synced nightly, `src/stocksvest_market/fundamentals.py`). Tested on 5B, none adopted |
| Sector-relative strength [FEAT-5] | **"Change vs its sector"** | `SECTOR_RELATIVE_CHANGE`: the stock's % change minus its sector's median |
| Market regime [FEAT-1, STRAT-5A/C/H] | **"Market trend (median change of all stocks)"** | `MARKET_CHANGE` (`MarketChangeAction`, `_market_change`). It takes the median % change of the whole ~880-stock universe over the period: every symbol passes or none does. **Not used**: it cut returns from +409% to +37..107% |

## 2. Automation settings

Automations page → automation editor. Models in `src/stocksvest_app/automations/models.py` (`OrderGuards`,
`RankingKey`), logic in `automations/planner.py` (`select`, `size`, `add_rank_data`) and `automations/runner.py`,
UI in `static/automations.js`.

### Order guards (one row of inputs in the editor)

| Concept | Label | How it works |
|---|---|---|
| Spread guard | **"max buy spread %"** | `maxSpreadPercent`. A buy whose live ask is more than X% above the bid is skipped (`WIDE_SPREAD`). 5B and Core 40 use 0.3% |
| Re-buy cooldown | **"no re-buy for … min after a sell"** | `rebuyCooldownMinutes`. Recently sold stocks are left out before the top N is taken, and the run shows "sold recently, left out: …". 5B uses 7 days |
| Require live price | Checkbox **"require live price"** | `requireLivePrice`. No live quote means the order is skipped (`NO_LIVE_PRICE`). Live now means an Alpaca quote |
| Volatility sizing [RISK-2] | **"size by volatility, target %"** | `targetVolatilityPercent`: buy size and position caps × target ÷ 20-day volatility (0.5–1.5). Best drawdown tool found (−45% → −38% with delisted), recommended, not applied live |
| Correlation limit [FEAT-9] | **"max correlation"** | `maxCorrelation`: TOP_N skips a stock whose 120-day returns correlate above X with a holding or earlier pick ("moves with a holding, left out"); market `GET /v1/returns` |
| Max positions [FEAT-3] | **"max positions"** | `maxOpenPositions`: a buy of a new symbol beyond it skips (`MAX_POSITIONS`) |
| Sector cap [RISK-1] | **"max per sector %"** | `maxSectorPercent`. Stocks of full sectors are skipped before the top N ("sector full, left out: …"), and a buy that would cross the cap shrinks or skips (`SECTOR_FULL`). **Not used** |

### Selection, sizing and schedules

| Concept | Where | How it works |
|---|---|---|
| Momentum ranking [FEAT-7] | Order action → TOP_N → rank by **MOMENTUM_12_1 / MOMENTUM_6_1** | The % change from 12 (or 6) months ago to 1 month ago. `planner.add_rank_data` fetches it before ranking. 5B uses 12-1 |
| Volatility / 52-week-high ranking | Rank by **VOLATILITY_20 / FROM_52W_HIGH** | From the indicators endpoint, via `planner.add_rank_data` |
| Most-traded ranking [RISK-3] | Rank by **AVERAGE_DOLLAR_VOLUME_20** | Dollars traded a day over 20 days (`market.volumes`). Core 40 uses it |
| 1-minute schedules | Schedule → MARKET → repeat every **1** minute | The old 5-minute minimum was removed |
| Position sizing by equity | Sizing "cash amount" + guard **"or %"** (max position %) | A large cash amount capped by the position limit means each buy is X% of the budget. 5B uses 5%, Core 40 uses 2.5% |

**Where to see what a run decided:** an automation → run history → a run. Each order shows its decision, the
skip reason, and the price with **"live"** (Alpaca quote) or **"bar"** (newest minute bar) next to it
(`priceSource` on each intent).

## 3. Market data

Market service code in `src/stocksvest_market/` (`alpaca.py`, `api.py`, `config.py`).

| Concept | Where to look | How it works |
|---|---|---|
| No delay | Prices are about a minute old. `GET /v1/status` → `dataDelayMinutes: 0` | Paid Alpaca plan, real-time SIP feed, synced every minute (`SIP_DELAY_MINUTES=0`, `SYNC_INTERVAL_MINUTES=1`, `ALPACA_RPM=1000`) |
| Live bid/ask | "live" next to prices on the portfolio page and in runs; `GET /v1/quotes` → `source: ALPACA` | `AlpacaQuotes` reads Alpaca's snapshots. A one-sided or crossed quote keeps only the last trade. The app treats `LIVE_SOURCES = {"ALPACA", "IBKR"}` as live (`stocksvest_app/market/client.py`) |
| IBKR live bars | — | Built, then **switched off** (`IBKR_ENABLED=false` in `deploy/tinkergw/docker-compose.yml`) when the subscription was cancelled. Not needed with real-time minute bars |
| 10 years of daily history [DATA-1] | — | `DAILY_HISTORY_DAYS=3650` on tinkergw: 1.74M daily bars back to Oct 2016. Hourly stays 2 years, minute bars 7 days |

## 4. Portfolios and budgets (Portfolios page)

| What | What you see |
|---|---|
| Momentum 5B [STRAT-5B] | $45k: "5B Buy" (Mondays 11:00, top 20 by MOMENTUM_12_1, liquid and 6-month change > 0) and "5B Sell" (daily 11:00, held > 30 days and down over 6 months) |
| Core 40 [RISK-3] | $45k: "Core 40 Buy" / "Core 40 Sell" (every 13 weeks on a Tuesday: sell all at 10:30, rebuy the top 40 by dollar volume at 11:00). First run 2026-10-27 |
| Paper portfolio → Testing | $10k. The old Buy/Sell are **paused**; its 19 positions sell at Monday's open |
| Position prices | The positions table shows "live" or "bar" next to each price |

## 5. Background fixes (you notice them by their absence)

| Concept | How it works |
|---|---|
| False "app and broker disagree" emails [OPS-2] | A sync now waits, up to 10 minutes per order (`FILL_LAG` in `orders/service.py`), for fills Alpaca reported but hasn't listed yet, before comparing cash and positions |
| "Live prices" alert email | Sent if Alpaca quotes fail during market hours, when orders that require a live price get skipped (`alerts/checks.py`) |
| Daily snapshot timing | Taken 5 minutes after the close (`performance/snapshots.py`) |
| Deploy from the Linux machine | `scripts/deploy-tinkergw.sh` builds natively on x86_64 (no buildx needed) |

## 6. Research tools (command line, not in the UI)

| Concept | Where |
|---|---|
| Backtester on real history [BT-1, BT-2] | `python -m stocksvest_app.backtest --app-db … --market-db … --buy … --sell … --start … --end …`. Options: `--fill NEXT_VWAP` (default) or `SAME_CLOSE`; `--steps SCHEDULE` (each automation on its own schedule, default) or `HOURLY`; `--spread-factor 2` for a cost stress test; `--overrides` to change settings for one run. Code in `src/stocksvest_app/backtest/` |
| Strict fills [BT-7] | `--fill NEXT_VWAP_STRICT`: before 2024 (daily bars only) the next day's VWAP, never a price from before the decision |
| What it models | Decisions use only data complete at that moment (`HistoricalMarket`). Fills are at the next hour's average price ± a modelled spread (the day's average before 2024). Fires snap to the start of the hour. Delisted positions close at their last price after 5 trading days without bars. The summary includes buy-and-hold benchmarks (`buyHold.allPct`, `buyHold.liquidPct`) |
| The strategies tested [STRAT-1..6] | `scripts/backtest_strategies.py` builds STRAT-1/2/3, the four 5B versions and CORE-40 in a scratch database |
| Sleeve and blend tests [RISK-3] | `scripts/backtest_sleeve.py` (top-N sleeves); `scripts/backtest_compare.py` (return, worst drop, years, budgets added together) |
| Delisted stocks [DATA-4] | A scratch copy only (1,172 extra stocks), deliberately never added to production |
| The to-do list | `TODO.md`: every result table and a permanent ID for each item |

## 7. Discussed but not in the app

- Random-walk simulations (20 graphs × 5 spreads): one-off scratch scripts, replaced by the real-history backtester.
- Graph theory / correlation clusters: FEAT-9, not built.
- Researcher / advisor: IDEA-1, parked.
- Volatility sizing, faster exits, momentum-crash signal, more robustness runs: RISK-2, RISK-4, RISK-5, BT-7, BT-8,
  not built.
- Trimming positions that grew too big: noted under RISK-1, not built.
