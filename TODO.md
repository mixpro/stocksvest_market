# TODO

Each item has a permanent ID. IDs are never reused or renumbered; finished items move to **Done** with their ID.
Status: `[ ]` open · `[~]` in progress · `[x]` done · `[-]` dropped.

Longer write-ups for an item can go in `todos/` (e.g. `todos/001-sec-user-agent-for-sectors.md`).

Prefixes: **RISK** drawdown control · **BT** backtester · **DATA** market data · **STRAT** strategies to test · **FEAT** new filter/automation features · **OPS** production and fixes · **IDEA** parked ideas.

Suggested order: BT-1 → BT-2 → STRAT-1..3 → DATA-1 → FEAT-1 → BT-4 → rest.

---

## RISK: Cutting STRAT-5B's worst drop (Kimi review 2, 2026-10-25)

5B's worst drop is deeper than buy-and-hold's: −38% (Jun 22 → Jul 29, 2026) and −45% with delisted stocks (Feb 2021 → Mar 2023), vs −31..−34% for buy-and-hold (Feb–Mar 2020). At the 2026 peak 5B was 84% technology (memory/storage/optical: TTMI, AXTI, WDC, SNDK, MU, STX…), winners grown to 10–12% each, then down 30–60% together.

- [x] **RISK-1: Sector cap** (Kimi's #1): new guard `maxSectorPercent` (sector value / budget equity); skip a buy that would cross it and take the next ranked stock. Test 25 / 30 / 40%, with and without delisted stocks, ×1 and ×2 spreads.
  - Also test trimming positions that grew above a cap (e.g. 10%), since the 2026 drop came from drift, not from the 5% buys.
  - Built 2026-10-25: guard `maxSectorPercent`. Results (no trimming):

    | Run | Total | /yr | Max drop | 2021 | 2026 |
    |---|---|---|---|---|---|
    | 5B | +409% | 19.9% | −38% | −12% | +40% |
    | cap 40% | +278% | 16.0% | −40% | −11% | +9% |
    | cap 30% | +295% | 16.5% | −39% | −13% | +9% |
    | cap 25% | +293% | 16.5% | −38% | −2% | −4% |
    | cap 30%, spreads ×2 | +418% | 20.1% | −32% | +5% | +10% |
    | 5B with delisted | +357% | 18.4% | −45% | −23% | +26% |
    | cap 40% / 30% / 25%, delisted | +339% / +349% / +293% | | −40% / −38% / −39% | | |

  - Verdict: no reliable cut in the worst drop on today's stocks, ~110 points less return; with delisted stocks −45% → −38..−40% at little cost. Holdings drift past the cap, so it mostly trims the upside. Not adopted.
- [x] **RISK-2: Volatility-scaled sizing**: buy size × clamp(0.40 / 20-day volatility, 0.5, 1.5), capped at 7.5%. Compare with plain 5B at the same average exposure.
  - Built 2026-10-25: guard `targetVolatilityPercent` (scales each buy and its position caps by target ÷ 20-day volatility, 0.5–1.5). Overnight batch, 2017-11..2026-10, $10k:

    | Run | Today's stocks | With delisted |
    |---|---|---|
    | 5B | +409%, 19.9%/yr, −38% | +357%, 18.4%/yr, −45% (2021 −23%) |
    | target 40% | +350%, 18.2%/yr, −38% | +344%, 18.1%/yr, −38% (2021 −1%) |
    | target 60% | +395%, 19.5%/yr, −37% | +364%, 18.6%/yr, −38% (2021 −3%) |
    | target 60% + maxCorrelation 0.7 | +335%, 17.8%/yr, −37% | +319%, 17.3%/yr, −37% |

  - Verdict: on today's stocks it changes nothing beyond noise; with delisted stocks (the honest test) target 60% keeps the return and cuts the worst drop −45% → −38% and 2021 from −23% to −3%. Cheapest drawdown cut found so far. Recommended for 5B (your call; not applied live).
- [x] **RISK-3: Blend with buy-and-hold**: live 2026-10-25 as Core 40 + 5B, $45k each: 25 / 50 / 75% 5B, rest in liquid buy-and-hold, rebalanced quarterly. The baseline every risk rule must beat.
  - Computed from the daily curves (no costs on the buy-and-hold sleeve):

    | 5B share | Today's stocks: /yr, max drop | With delisted: /yr, max drop |
    |---|---|---|
    | 100% | 19.9%, −38% | 18.4%, −45% |
    | 75% | 19.8%, −35% | 18.2%, −36% |
    | 50% | 19.5%, −35% | 17.6%, −34% |
    | 0% (buy-and-hold) | 18.1%, −34% | 15.5%, −31% |

  - Keeps almost all of 5B's return with buy-and-hold's drawdown: far better than the sector cap.
  - Open: how to hold the buy-and-hold sleeve live (no fractional shares; ~250 liquid stocks). Options: a halal ETF (SPUS/HLAL, needs ETFs in the universe), or the 30–50 largest liquid stocks equal-weight (test it).
  - Tested 2026-10-25: sleeve = the N most-traded stocks (20-day dollar volume, price > $10), equal weight, re-picked each quarter, spread costs at each rebalance:

    | | Today's stocks | With delisted |
    |---|---|---|
    | 5B alone | 19.9%/yr, −38% | 18.4%/yr, −45% |
    | top 30 sleeve | 21.4%/yr, −37% | 20.8%/yr, −38% |
    | **top 40 sleeve** | **21.7%/yr, −33%** | **21.0%/yr, −33%** |
    | top 50 sleeve | 21.2%/yr, −32% | 20.4%/yr, −31% |
    | 50/50 5B + top 40 | 21.3%/yr, −32% | 20.4%/yr, −33% |

  - The sleeve alone beats 5B and equal-weight buy-and-hold: 2017–2026 was a big-company decade, so this is partly era luck. The blend keeps it diversified across two different approaches.
  - As real automations ("CORE-40" in `scripts/backtest_strategies.py`: TOP_N 40 by AVERAGE_DOLLAR_VOLUME_20, 2.5% each; every 13 weeks sell all at 10:30, rebuy at 11:00; the sell needs maxOrdersPerRun 100), $45k each:

    | | Today's stocks | With delisted |
    |---|---|---|
    | 5B | 21.0%/yr, −38% | 18.0%/yr, −40% |
    | Core 40 | 20.4%/yr, −34% | 19.7%/yr, −34% |
    | **5B + Core 40 (two budgets)** | **20.7%/yr, −33%** | **18.9%/yr, −32%** |

  - To run it live: a ranking key for 20-day dollar volume, a quarterly sell-all + rebuy (liquid names: tiny spreads), and fractional shares (the app sizes in whole shares; Alpaca supports fractions) or a sleeve big enough for whole shares.
- [x] **RISK-4: Faster stock exit**: (a) drop the 30-day minimum, (b) 3-month change < 0, (c) 3- or 6-month < 0. Check what sold stocks did in the next 3 months.
  - Results (today's / with delisted): (a) no minimum +386% −40% / +356% −40%; (b) 3-month +353% −42%; (c) 3- or 6-month +424% −41% / +296% −42%; below 200-day average +340% −44%; also sell when 25% below the 52-week high: see BT-4.
  - Verdict: no exit beats the current one; faster exits trade more and buy back late. Keep "held > 30 days and 6-month < 0".
- [-] **RISK-5: Momentum-crash signal** (only after an event study): top-20 momentum names trailing the universe by > 10% over 3 months → halve new buys for 4 weeks. Kimi: the market-regime exit failed because it's slow and all-or-nothing.
  - Event study 2018-11..2026-10, 375 Mondays: the top 20 by 12-1 momentum beat all liquid stocks by +1.4 pts over the next 4 weeks and +3.8 over 13 on average. Kimi's signal fired once (2021-05; next 4 weeks +5.2); the Daniel & Moskowitz "bear rebound" signal 3 weeks (2023-11/12; 4 weeks −1.8, 13 weeks +6.8); market 6-month < 0 on 76 weeks: momentum did better after them (+2.3 / +6.1).
  - Dropped: no crash in this period to protect against, and nothing to fit a rule on. Revisit only if a real momentum crash (like 2009) shows up.
- [x] **BT-7: Audits Kimi asked for**: list the 27 delisted exits (buyout vs failure; stress failures at −100%); before 2024 the fill is the decision day's VWAP, which includes trades before the decision (use the next day's to be strict).
  - Delisted exits: all 27 were buyouts/mergers (Immunomedics +176%, MyoKardia +142%, Medicines Co +53%, Inphi, Red Hat, Cimarex, Whiting…), one meme merger (Support.com −68%). No failures: the liquidity + uptrend gate never held a collapsing stock. Stress cost: 0.
  - Strict fills (new fill mode `NEXT_VWAP_STRICT`: before 2024, the next day's VWAP): 5B +409% → +322% (17.4%/yr, below buy-and-hold's 18.1%) on today's stocks; +357% → +333% (17.7%/yr vs 15.5%) with delisted. Part of the edge on today's stocks came from same-day fills; with delisted stocks it holds (+2.2 pts/yr). The truth is between the two modes.
- [ ] **BT-8: How fragile is the edge**: 100 runs with random entry weekdays; block bootstrap of monthly 5B-minus-benchmark returns; walk-forward (choose on 2017–2020, test 2021–2023, etc.). Report drawdown and volatility next to return for 5B and the benchmark.

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
- [x] **BT-4: Test entries and exits separately**
  - 5B, 2017-11..2026-10 (375 buys, 357 sells), forward returns vs the liquid universe on the same day:
    - Entries: +1 day +0.2 pts, +5 days +0.3, +20 days −0.6, +60 days +3.0 average but only +0.4 median; beat 47–52% of the time. The edge is a few big winners (right skew), not a high hit rate.
    - Exits: sold stocks did +1.9 / +2.2 pts better than the universe over the next 20 / 60 days on average (median ~0): the exit costs a little on average, mostly by selling some recoveries.
    - MAE/MFE: median worst point −11%, best +21%, result −1%. Median give-back from the best point 25 pts; 66 of 181 trades that were +20% at some point closed below +5%.
  - A trailing exit (also sell when 25% below the 52-week high) was tested in the next batch: see 5B-X-TRAIL25 below.
  - Entries: average forward 1/5/20/60-day return of what each buy rule picks, vs. the universe.
  - Exits: same entries, different sell rules. Also what a stock did 1/5/20 days after we sold it.
  - MAE/MFE per trade (worst and best point while held).
  - Why: "stops lose, take-profits win" is partly an accounting artifact. A stop only closes losers by definition.
- [ ] **BT-5: Robustness checks**
  - 2× spread cost stress.
  - Walk-forward: tune on one period, test on the next.
  - Keep the last 12 months untouched until a strategy is final.
  - Block bootstrap of daily returns for a confidence range.
- [-] **BT-6: Minute-resolution runs**: dropped 2026-10-25. No live strategy uses minute rules any more (the old setup is retired); 5B and Core 40 trade weekly/quarterly at 11:00, which hourly steps model. Revisit with DATA-2 if a minute rule comes back
  - Minute rules (F.S 3's 3-minute step) can't be tested at hourly steps.
  - Limit: only ~7 days of minute bars are kept. Needs DATA-2 first.

## DATA: Market data

- [x] **DATA-1: Keep ~10 years of daily bars**
  - Today `DAILY_HISTORY_DAYS=730` (2 years), so a 1-year rule has only 1 year to be tested on.
  - Alpaca's free plan serves daily bars back to 2016. Change the setting, re-sync, check DB size.
  - Caveat: survivorship bias. The universe is today's halal list, so stocks that died or delisted are missing.
  - Done 2026-10-25: `DAILY_HISTORY_DAYS=3650` in tinkergw `.env`, one-time full daily re-download (1.74M bars from 2016-10-28; 521 symbols in 2016 → 881 today). DB 671 → 784 MB. Hourly stays 2 years; backtests before 2024 fill at the day's VWAP.
- [-] **DATA-2: Keep more minute bars** (only if BT-6 is wanted): dropped with BT-6 (2 years of minute bars ≈ 250M rows, 15–20 GB, for no current use)
  - Check DB size impact first.
- [x] **DATA-4: Delisted stocks for the backtests (survivorship bias)**
  - The biggest doubt on every result: the universe is today's list, so stocks that collapsed or delisted are missing.
  - Check whether Alpaca serves bars for inactive assets; if so, add the ones that were halal-eligible and rerun STRAT-5B.
  - Done 2026-10-25: Alpaca has bars for 1,172 delisted NASDAQ/NYSE stocks (2016–2026), merged into a scratch copy only (2,083 stocks). No halal record exists for them, so all are in: an upper bound of the bias.
  - Backtester: a held stock without bars for 5 trading days is sold at its last close; the benchmark counts delisted stocks at their last close.
  - 2017-11..2026-10:

    | | Today's stocks only | With delisted stocks |
    |---|---|---|
    | STRAT-5B | +409% (19.9%/yr) | +357% (18.4%/yr), max drawdown −45% |
    | Buy-and-hold, liquid | +345% (18.1%/yr) | +264% (15.5%/yr) |
    | Buy-and-hold, all | +330% | +146% |

  - The bias flattered buy-and-hold far more than 5B: its edge grows from ~1.8 to ~2.9 points a year. 5B's 27 delisted positions made +$2,826 (22 wins: mostly buyouts at a premium).
- [x] **DATA-3: Buy side is still 16 min delayed**: Alpaca Algo Trader Plus ($99/month) since 2026-10-25. Defaults now `SIP_DELAY_MINUTES=0`, `SYNC_INTERVAL_MINUTES=1`, `ALPACA_RPM=1000`
  - Option 1: Alpaca paid plan (real-time SIP).
  - Option 2: IEX feed top-up (free, real-time, thinner coverage).
  - Held symbols are already live via IBKR (done).

- [x] **DATA-5: Alpaca real-time quotes as backup for IBKR**: became the main source on 2026-10-25, when the IBKR data subscription was cancelled. `IBKR_ENABLED=false` in production; quotes come from Alpaca SIP snapshots (`source: ALPACA`)
  - The paid plan includes real-time bid/ask. Today bid/ask come only from the IB Gateway: if it's down, orders with `requireLivePrice` (5B Buy) skip with NO_LIVE_PRICE.
  - Use Alpaca's latest quote when IBKR has none (or as the main source).

## STRAT: Strategies to backtest (from Kimi)

Built by `scripts/backtest_strategies.py`. Results 2025-10-24..2026-10-23 (liquid buy-and-hold +18.7%, live setup −90%):

| | Return | Max drawdown | Trades | Win rate | Avg invested |
|---|---|---|---|---|---|
| STRAT-1 | +9.2% | −$2,332 | 116 buys | 37% | $10.1k |
| STRAT-2 | **+23.5%** | −$1,475 | 396 buys | 47% | $8.0k |
| STRAT-3 | +0.6% | −$384 | 318 buys | 47% | $1.4k |

One year of a rising market only: needs DATA-1 (10 years, incl. 2022) before trusting any of it.

**9 years, 2017-11-01..2026-10-23** ($10k each; buy-and-hold of liquid stocks, equal weight, no costs):

| Year | STRAT-1 | STRAT-2 | STRAT-3 | Live setup | Buy-and-hold |
|---|---|---|---|---|---|
| 2017 (Nov–Dec) | −0.4% | −0.8% | +0.5% | −13.0% | +4.1% |
| 2018 | +1.2% | −4.4% | −2.8% | −60.1% | −4.7% |
| 2019 | +17.4% | +4.3% | −1.0% | −52.9% | +36.2% |
| 2020 | +24.2% | +40.5% | +5.8% | −58.7% | +36.0% |
| 2021 | +9.3% | +4.9% | +4.2% | −50.3% | +26.9% |
| 2022 | −5.1% | −2.2% | +0.3% | −29.6% | −17.9% |
| 2023 | +0.3% | +7.4% | +5.0% | −44.6% | +31.6% |
| 2024 | +12.5% | +12.9% | −0.6% | −28.2% | +15.1% |
| 2025 | +1.2% | +16.1% | +5.6% | −43.4% | +20.6% |
| 2026 (to Oct 23) | +17.8% | +10.7% | +0.8% | −22.8% | +15.5% |
| **Total** | **+105%** | **+122%** | **+19%** | **−99.6%** | **+345%** |

- None beats holding. STRAT-2's one-year win was luck of the window. Their only edge: small losses in 2022 (mostly in cash).
- Buy-and-hold is inflated by survivorship bias (today's universe; only 542 of them had data in 2017).
- Live setup lost money every single year.

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
- [x] **STRAT-5: Hold liquid stocks, step aside in crashes**
  - From the 9-year result: staying invested beats every rule set; the strategies only helped by being in cash in 2022.
  - Test: buy-and-hold of liquid stocks plus a market-regime exit (needs FEAT-7) and a momentum ranking (FEAT-1).
  - Built: liquid gate + stock 6-month > 0, weekly Monday 11:00, top 20 by MOMENTUM_12_1, 5% of equity per buy.
  - 9 years (2017-11..2026-10), buy-and-hold +345%:

    | Variant | Total | Per year | Max drawdown | Avg invested |
    |---|---|---|---|---|
    | **5B: no regime, sell when held > 30 days and own 6-month < 0** | **+409%** | ~19.8% | −38% | 97% |
    | 5A: + sell all when market 6-month < 0 | +107% | | −39% | 67% |
    | 5C: + market 3-month regime | +37% | | −46% | 59% |
    | 5H: market regime exit only | +42% | | −41% | 68% |

  - The regime exit sells low and buys back late: it cost far more than it saved in 2022.
  - 5B beats buy-and-hold by ~1.7%/year (19.8% vs 18.1%). Caveats: survivorship bias likely flatters momentum most; best of 4 variants picked after seeing results; 2021 was a −12% year (momentum crash).
- [x] **STRAT-6: Robustness of STRAT-5B before any live use** (BT-5)
  - 2× spread cost; top 10 / 30 instead of 20; Wednesday instead of Monday; MOMENTUM_6_1.
  - If it holds: paper-trade it on its own budget next to (or instead of) the live setup.
  - Results 2017-11..2026-10 (buy-and-hold +345%, ~18.1%/year):

    | Run | Total | Per year | Max drawdown |
    |---|---|---|---|
    | 5B as built | +409% | 19.9% | −38% |
    | Spreads ×2 | +577% | 23.7% | −38% |
    | Spreads ×3 | +247% | 14.9% | −38% |
    | ~10 positions (10% each) | +535% | 22.9% | −42% |
    | ~30 positions (3.3% each) | +344% | 18.1% | −38% |
    | Buy Wednesday | +283% | 16.1% | −43% |
    | MOMENTUM_6_1 | +497% | 22.0% | −39% |

  - 4 of 7 beat buy-and-hold, median +409%. But noise is as big as the edge: Monday vs Wednesday alone moves it 126 points, and ×2 spreads "beat" ×1 (the spread guard changed which stocks were picked).
  - Verdict: about as good as buy-and-hold, maybe slightly better, same drawdowns. Not a proven edge. Same pattern every run: big in 2020/2024/2026, lags in 2021/2023.
- [~] **STRAT-7: Paper-trade STRAT-5B** (your decision)
  - 2026-10-25: 5B budget raised to $45k. Core 40 live next to it: portfolio `pf_ad0836d9e91b4c60a035c04573f9e6ac` ($45k), budget `bud_c490c8288f1f4550b14ccdd2658a7f75`, "Core 40 Buy" (`aut_f267bfa5…`, every 13 weeks Tue 11:00 ET, first 2026-10-27) and "Core 40 Sell" (`aut_70adf4c2…`, same days 10:30). Paper portfolio keeps $10k (Testing).
  - Core 40's first picks: MU NVDA SPCX META TSLA AAPL SNDK AMD MSFT INTC AMZN GOOGL AVGO GOOG … (GOOG and GOOGL are both in: Alphabet gets 5%).
  - Live since 2026-10-25 (deploy 6521b88): portfolio "Momentum 5B" (`pf_059512a438f34e0ca2439864f7f87d5b`, $10,000 moved from Paper portfolio), budget `bud_11366221bbc3485b9ca9da8e4776e753`.
  - "5B Buy" (`aut_c6458dc7…`) Mondays 11:00 ET, "5B Sell" (`aut_35e5c29a…`) daily 11:00 ET. Data-age guard 30 min (as the old Buy).
  - First run Monday 2026-10-26 11:00 ET. Sunday preview: 229 matches, top 20 SNDK, MU, STX, AMD, DELL, INTC…
  - The old Testing Buy/Sell were paused 2026-10-25. Their 19 positions (~$7.1k) have market-on-open (OPG) sells for Monday 2026-10-26 09:30 ET.
  - Own budget on the Alpaca paper account, next to or instead of the live setup (which lost every year in 9).
  - Goal: check live fills/spreads vs the backtest, not to prove the edge (months of paper can't).
- [x] **STRAT-4: Decide what to do with the live setup**: paused 2026-10-25, positions sold at Monday's open, replaced by STRAT-5B (STRAT-7)
  - Backtest verdict so far: live setup −71%, spike-chasing rules removed −9%, benchmark +20%.
  - After STRAT-1..3: replace, keep paper-testing, or turn off.
  - Cleanup: R.B E is redundant in the Buy group union.

## FEAT: New features (ranked by value for effort)

- [x] **FEAT-1: Momentum ranking key (12-1 and 6-1)**: `rankBy` `MOMENTUM_12_1` / `MOMENTUM_6_1` (2026-10-25)
  - Return over the last 12 (or 6) months, skipping the latest month. Strongest known selection signal.
  - Today `rankBy` only has daily change, price, volume.
- [x] **FEAT-2: Volatility (ATR)**: market `GET /v1/indicators` (20-day volatility, 14-day ATR %), filter step `INDICATOR`, ranking key `VOLATILITY_20`, guard `targetVolatilityPercent` (2026-10-25). A volatility cap (< 60%) cut 5B to +259% / +198% with delisted: not useful as a filter; as sizing see RISK-2
  - Filter step on ATR %, position sizing by volatility, ATR-based stops.
- [x] **FEAT-3: Portfolio limits**: guard `maxOpenPositions` (MAX_POSITIONS), with `maxSectorPercent` (RISK-1) and `maxOrdersPerRun` already there (2026-10-25). 5B with max 20 positions: +396% / +360%: no change (it rarely holds more than ~20). A per-day turnover cap: not built (schedules + maxOrdersPerRun cover it)
  - Max open positions, max per sector, max buys/sells per day (turnover cap).
- [x] **FEAT-4: Trend position**: `INDICATOR` `PRICE_VS_SMA_50`, `PRICE_VS_SMA_200`, `FROM_52W_HIGH`; ranking key `FROM_52W_HIGH` (2026-10-25). 5B + above 200-day average: +384% / +376% (no change); + within 15% of the high: +211%; ranked by nearness to the 52-week high (George & Hwang) instead of 12-1: +223% (13.9%/yr) but max drop only −27% (−20% with delisted); 50/50 with 5B: 17.2%/yr, −33%
  - 200-day moving average (price above/below), distance from 52-week high.
- [x] **FEAT-5: Sector-relative strength**: filter step `SECTOR_RELATIVE_CHANGE` (stock's change minus its sector's median) (2026-10-25). 5B + 6-month sector-relative > 0: +423% (20.2%/yr, −39%): within noise. With-delisted test not valid (delisted stocks have no sector, so the step drops them)
  - Stock's change vs. its sector's average over the same period.
- [ ] **FEAT-6: Earnings calendar**
  - Skip buys N days before earnings; needs a data source.
- [x] **FEAT-7: Market regime**: `MARKET_CHANGE` filter step, median % change of the whole universe (2026-10-25). Backtests: it hurt (see STRAT-5)
  - Only buy when the broad market is above its 200-day average (or similar).
- [ ] **FEAT-8: Quality fundamentals**
  - Profitability, debt. Needs a data source.
- [x] **FEAT-9: Correlation clusters** (the graph-theory idea): guard `maxCorrelation` + market `GET /v1/returns` (2026-10-25): a TOP_N buy skips a stock whose 120-day daily returns correlate above X with a holding or an earlier pick (tooCorrelated). 5B with 0.7: +322% −39% / +360% −38% with delisted; with 0.5: +191% / +133%. Cuts the delisted drawdown like volatility sizing, but costs return on today's stocks
  - Group stocks that move together; hold at most N per cluster.
  - Kimi ranked this low for returns, useful for diversification.

## OPS: Production and fixes

- [ ] **OPS-1: Check live bars Monday 2026-10-26 after 09:30 ET**
  - Also: did 5B Buy place ~20 orders at 11:00 ET, and do the fill prices sit within the modelled spread?
  - And: did the 19 OPG sells of the Testing budget fill at the open?
  - `GET /v1/live` → `lastBarAt` should update every few seconds for held symbols.
- [x] **OPS-2: False cash-mismatch alerts** (fixed 2026-10-25: FILL_LAG in orders/service.sync_account)
  - Sync compares app vs. broker while fills are only partly imported.
  - Fix: skip the cash/position comparison while the broker reports fills not yet imported.
- [x] **OPS-3: `claude-lite` defaults to DeepSeek**: `ANTHROPIC_MODEL` now defaults to Kimi in `~/.local/bin/claude-lite` (`--model haiku` still gives DeepSeek) (2026-10-25)
  - Add `export ANTHROPIC_MODEL="my-kimi-k3.1-pro-highspeed"` to `~/.local/bin/claude-lite`, or keep using `--model sonnet`.
- [x] **OPS-4: Ponytail statusline badge** (optional): `statusLine` added to `~/.claude/settings.json` (2026-10-25)

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
