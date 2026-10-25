# Research ideas (2026-10-26)

New ideas to improve the system, from a literature search and two rounds of discussion with Kimi. **Nothing here is
built yet.** Each idea has why it should work, its limits, and how we would test it. Kimi's opinions are advice;
where they were checked, it says so.

Starting point (see TODO.md): 5B makes ~19–21%/yr with a −38..−45% worst drop and ~31%/yr volatility, a modest
edge over equal-weight buy-and-hold (+1..+5 pts/yr, 85% bootstrap). Variant picking by past results proved to be
noise (BT-8 walk-forward), so every idea below must beat the **current best combination** (5B + Core 40, with
volatility sizing as the candidate), net of costs, not just plain 5B.

## Ranked shortlist

| Rank | ID | Idea | Kind | Effort |
|---|---|---|---|---|
| 1 | IDEA-2 | Research gate: frozen specs, experiment ledger, deflated results | method | low |
| 2 | IDEA-3 | Split buys into tranches on different days (timing luck) | portfolio construction | low |
| 3 | IDEA-4 | Halal ETF as the real-money core | portfolio construction | low–medium |
| 4 | IDEA-5 | Core 40 banding: keep holdings still in the top 60 | turnover | low |
| 5 | IDEA-6 | Residual (idiosyncratic) momentum ranking | signal | medium |
| 6 | IDEA-7 | Lottery-stock (MAX) exclusion, against frog-in-the-pan | signal / risk | low |
| 7 | IDEA-8 | Earnings-announcement return (EAR) tilt | signal | medium |
| 8 | IDEA-9 | Don't chase: skip the most extended 5-day movers | entry timing | low |
| 9 | IDEA-10 | Quote-aware execution | execution | medium |
| 10 | IDEA-11 | Asset-growth exclusion | signal | medium |
| 11 | IDEA-12 | Momentum lookback ensemble | robustness | low |
| 12 | IDEA-13 | Industry momentum | signal | medium |

---

## IDEA-2: Research gate (frozen specs, ledger, deflated results)

- **What:** before running any new test, write down the rule, the one main metric and the pass bar. Keep a ledger of every
  run, including failures. Judge a winner after counting how many things were tried, using a deflated Sharpe ratio or a
  bootstrap "reality check". Promote only what beats the current blend under normal **and** doubled costs, and with
  neighbouring parameters too.
- **Why it makes sense:** we have run 40+ variants on the same nine years, and the walk-forward showed the "best"
  variant ranks near last on the next period. With that many tries, some will look great by luck
  (Harvey, Liu & Zhu 2016; Bailey & López de Prado's deflated Sharpe). 5B's edge comes from a few big winners, which
  makes this worse.
- **Limits:** nothing makes 2017–2026 untouched data again. Trial counts are approximate. Months of paper trading can't
  prove a 20%/yr edge at 31% volatility.
- **Test:** none needed, it's a process. Start the ledger from TODO.md (every result is already there), and add a
  "pass bar" line to each new TODO item before running it.

## IDEA-3: Tranches on different days (rebalance timing luck)

- **What:** split 5B's money into 5 sub-budgets, each buying its own top 20 on a different weekday. Split Core 40 into 3
  sub-budgets that rebuild in different months.
- **Why it makes sense:** in BT-8 the same strategy made 15.7%–23.5%/yr depending only on the buy weekday and start
  week, and Monday vs Wednesday alone moved the 9-year result by 126 points. That spread is luck of the calendar, not
  skill. Spreading entries averages it out ("rebalance timing luck", Hoffstein et al.). Expect a similar average with a
  much narrower range: the live result becomes closer to the backtest median.
- **Limits:** the average return doesn't rise and market risk doesn't fall. More, smaller orders (whole shares: at $9k
  per tranche a 5% position is $450). Each tranche needs its own cash and holding clock (the app already scopes holdings
  per budget). Kimi: judge the combined equity curve, not the average of the tranches' returns.
- **Test:** the app already supports this: 5 budgets with copies of the 5B automations on MON..FRI. Backtest the sum of 5
  runs (`compare.py` adds budgets) against the 100-run distribution. Pass bar: median similar, range at least 50%
  narrower.

## IDEA-4: Halal ETF as the real-money core

- **What:** hold a Shariah-screened ETF (SPUS: S&P 500 Shariah, 0.45%; HLAL: FTSE USA Shariah, 0.50%) as the core
  instead of, or next to, Core 40.
- **Why it makes sense:** a known fee buys diversification (~200 stocks), professional screening and rebalancing, and no
  dependence on our model or code. Kimi's top real-money pick. Core 40 is our own concentrated, rule-made version of
  the same idea.
- **Limits:** fees 15× a plain S&P ETF. 40–70% in the top 10 holdings. It doesn't cut market crashes. The market service
  excludes ETFs today (universe = halal common stocks/ADRs), so the app can't trade it yet. Its screening may differ
  from your halal source.
- **Test:** let the market service carry a short allow-list of halal ETFs (bars + quotes), then backtest buy-and-hold SPUS
  (since 2019) and HLAL against Core 40 over the same window.

## IDEA-5: Core 40 banding

- **What:** at each quarterly rebuild, keep holdings still ranked within the top 60 by dollar volume, sell only the rest,
  and refill to 40 from the new top 40.
- **Why it makes sense:** today Core 40 sells all 40 and buys back ~30 of the same stocks, paying the spread twice for
  nothing. Banding (a higher bar to enter than to stay) cuts turnover while keeping the signal (FAJ 2019 study on cost
  mitigation; MTUM uses buffer rules). 5B doesn't need it: it already keeps a stock that falls out of the top 20 until
  its trend turns.
- **Limits:** weights drift toward past winners. Kimi: keep equal-weight targets with a no-trade tolerance rather than
  liquidating to reset. A slightly staler list (ranks 41–60).
- **Test:** needs a "rank" filter step (keep held symbols whose rank is within N) or a sell filter "not in top 60". Then
  compare turnover and net return with the current Core 40.

## IDEA-6: Residual (idiosyncratic) momentum

- **What:** rank by each stock's 12-1 return **after removing what the market and its sector explain** (regress its
  monthly returns on the market and sector over 36 months; rank by the sum of residuals over months 12..2, divided by
  their volatility).
- **Why it makes sense:** plain momentum loads on whatever factor or sector has been hot (our 84% technology peak in
  2026), and that shared bet is what crashes. Blitz, Huij & Martens (2011) and later Robeco work report about the same
  return with about half the volatility and milder crashes. It targets our biggest weakness (31% volatility, −45%).
- **Limits:** needs 3 years of history per stock, so new listings are left out. The published volatility cut is for
  diversified long-short portfolios; for 20 long-only stocks it may overlap the volatility sizing that already helps.
  Kimi: test with sizing held fixed, compare at equal risk.
- **Test:** a new ranking key `RESIDUAL_MOMENTUM` (computed from daily returns we already have). One frozen run on both
  universes, normal and strict fills.

## IDEA-7: Lottery-stock (MAX) exclusion, against frog-in-the-pan

- **What (MAX):** skip buy candidates whose largest one-day gain in the last month is in the top 10% of the eligible
  stocks (or the average of the top 5 daily gains, which is less noisy).
- **What (frog-in-the-pan, the alternative):** prefer momentum built from many small up days over a few jumps:
  ID = sign(12-1 return) × (% down days − % up days); skip the most "jumpy" fifth.
- **Why it makes sense:** stocks with lottery-like spikes attract speculators and then underperform (Bali, Cakici &
  Whitelaw 2011: >1%/month between the lowest and highest MAX deciles). Investors underreact to gradual news, so smooth
  momentum persists longer (Da, Gurun & Warachka 2014). Our 2021 losses were exactly such spike stocks (BEEM, SPRT,
  CPSH).
- **Limits:** part of MAX is earnings-day jumps, so excluding them can drop real news. 5B's edge **is** a few big winners,
  and both filters may cut them. Kimi: test them head to head, don't stack them and count two wins, and measure how many
  eventual top winners each removes.
- **Test:** the indicators endpoint gets `max1mPercent` and `infoDiscreteness`, the INDICATOR step does the rest. One
  cut-off each, decided in advance.

## IDEA-8: Earnings-announcement return (EAR) tilt

- **What:** after each earnings release (8-K item 2.02, already synced), measure the stock's 3-day return around it minus
  the market's. Among the momentum top 40, prefer stocks whose last release (≤ 60 trading days ago) had a positive EAR.
- **Why it makes sense:** the market under-reacts to earnings news (post-earnings drift; Bernard & Thomas). Brandt et al.
  (2008) show the announcement return captures the surprise, including guidance, without analyst data. It is more
  independent of price momentum than another price filter. Kimi preferred EAR to a net-income SUE (which needs reliable
  quarterly earnings and careful Q4 and restatement handling).
- **Limits:** the drift is weaker than it used to be (Martineau 2021 argues it's gone in large caps). Release timing
  (before the open or after the close) must be aligned. Our earlier earnings filters (FEAT-6) hurt, though they avoided
  earnings rather than using the news.
- **Test:** event study first (20/60-day forward returns of momentum stocks by EAR sign), then the overlay if it holds.

## IDEA-9: Don't chase (short-term reversal on entries)

- **What:** for new buys only, skip candidates whose 5-day return relative to the eligible median is in the top fifth;
  take the next-ranked stock. Never sell because of it.
- **Why it makes sense:** one-week winners tend to give some back (Jegadeesh 1990; Lehmann 1990: temporary price
  pressure). BT-4 showed 5B's buys lag the universe over the first 20 days (−0.6 pts), so we often buy at local peaks.
- **Limits:** reversal is strongest in illiquid stocks (ours are liquid). Strong weeks can be real news. It overlaps
  IDEA-7.
- **Test:** one specification (5 days, top fifth), next-session fills, normal and doubled costs. Compare after IDEA-7.

## IDEA-10: Quote-aware execution (real-time data)

- **What:** decide after the close; place buys during 10:00–15:45 as marketable limits capped at the ask (sells floored at
  the bid), re-price once after 60 s; postpone a discretionary buy one day when the quoted spread exceeds 0.25%. Never
  delay a risk exit.
- **Why it makes sense:** it improves what we keep (slippage) rather than chasing an uncertain return premium, and uses
  the live bid/ask we now pay for. Strict fills (BT-7) showed execution assumptions alone move 5B by ~2 pts/yr.
- **Limits:** limits can miss fills, and a missed momentum entry can cost more than the spread saved. Paper fills don't
  show queue position.
- **Test:** prospective only. Run half the orders each way on paper and measure fill vs arrival midpoint, counting unfilled
  orders as cost.

## IDEA-11: Asset-growth exclusion

- **What:** exclude the fastest-growing fifth of companies by annual total-asset growth (from 10-K/20-F, when public).
- **Why it makes sense:** companies that grow assets fastest (acquisitions, capex binges) later underperform (Cooper, Gulen
  & Schill 2008: over-investment, extrapolation). It's an investment signal, not another profitability screen (FEAT-8
  failed).
- **Limits:** assets miss intangibles and R&D, acquisitions distort them, annual data is stale, and it may again cut
  winners.
- **Test:** we already store assets (point-in-time). Sort first, overlay second, and check sector-adjusted.

## IDEA-12: Momentum lookback ensemble

- **What:** rank by the average of the 3-, 6-, 9- and 12-month (skip-1) momentum ranks.
- **Why it makes sense:** 6-1 gave +497% and 12-1 +409%: that gap is noise, and averaging lookbacks removes the bet on
  one parameter.
- **Limits:** the horizons overlap heavily (not real diversification) and add shorter-term turnover. Kimi: a robustness
  candidate, not a default.
- **Test:** one frozen run; adopt only if consistent across sub-periods and costs.

## IDEA-13: Industry momentum

- **What:** hold the best momentum stocks only from the 3 strongest industries (6-month industry return).
- **Why it makes sense:** much of stock momentum is industry momentum (Moskowitz & Grinblatt 1999). A peer-network
  version (Misirli, Scida & Velikov) reports Sharpe 0.65.
- **Limits:** the halal screen thins industries (few financials). It concentrates further (the opposite of the sector
  cap). The network version needs relationship data we don't have.
- **Test:** sector-level ranking with SIC sectors; compare with 5B at equal volatility.

---

## Considered and dropped

| Idea | Why dropped |
|---|---|
| Overnight momentum (Lou, Polk & Skouras 2019) | Momentum profits do accrue overnight, but that doesn't make a profitable long-only overnight ranking. Adjusted opens and open/close execution costs complicate it. Keep as a diagnostic only |
| Calendar-month seasonality (Heston & Sadka 2008) | Only ~9 same-month observations per stock in our data; too noisy, heavy multiple testing |
| Strategy-level constant volatility (Barroso & Santa-Clara 2015) | Without leverage it just holds cash after volatility rises; overlaps volatility sizing; out-of-sample evidence contested (Cederburg et al. 2020) |
| LLM headline sentiment (Lopez-Lira & Tang) | Can't be trusted in a backtest (model training leakage, timestamps); the edge shrinks as adoption grows. Only as a frozen forward experiment on Alpaca news |
| Short interest / days-to-cover | FINRA data is twice a month, delayed and missing for delisted stocks; the effect is strongest in small, illiquid stocks we don't trade |
| Hierarchical risk parity | ~20 long-only holdings give unstable correlation trees; mixed evidence against equal weight; correlation caps already failed |
| Customer–supplier momentum (Cohen & Frazzini 2008) | Needs relationship data we don't have; the effect weakened after publication and is partly a size effect |

## Also from Kimi: what could still flip the conclusions

- **The universe is today's halal list for the whole history.** A point-in-time halal history would be the best single
  data improvement, if the halal source keeps one.
- **Execution:** next-day VWAP is a delay test, not a live fill. Compare live paper fills against the backtest
  (OPS-1 onward).
- **Benchmark integrity:** the benchmark gets the same delisted treatment as 5B (checked: it does).
- **Price gates on adjusted prices:** found and measured (BT-9).

## Sources

- Blitz, Huij & Martens, residual momentum (via Robeco):
  [Robeco: taming momentum crashes](https://www.robeco.com/en-uk/insights/2023/02/quant-chart-taming-momentum-crashes),
  [UCP thesis on scaled residual momentum](https://repositorio.ucp.pt/bitstreams/be7fa383-f5c7-406b-b753-4ea8e49d5d23/download)
- Moreira & Muir, volatility-managed portfolios: [NBER w22208](https://www.nber.org/papers/w22208.pdf);
  out-of-sample critique summary: [Lehigh paper](https://www.lehigh.edu/~xuy219/research/COWY.pdf)
- Lou, Polk & Skouras, overnight vs intraday: [LSE paper](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf),
  [Alpha Architect summary](https://alphaarchitect.com/2015/02/overnight-momentum-vs-intraday-momentum/)
- Moskowitz & Grinblatt, industry momentum: [IDEAS](https://ideas.repec.org/a/bla/jfinan/v54y1999i4p1249-1290.html);
  network/peer momentum: [Misirli, Scida & Velikov](https://business.pitt.edu/wp-content/uploads/2022/03/MSV.pdf)
- Heston & Sadka seasonality: [UMD summary](https://rhsmith.umd.edu/news/seasonality-stock-market-returns),
  [JFQA international evidence](https://ideas.repec.org/a/cup/jfinqa/v45y2010i05p1133-1160_00.html)
- Post-earnings drift today: [Martineau, "Rest in Peace"](https://cfr.ivo-welch.info/published/papers/martineau2021rest.pdf)
- MAX / lottery stocks: [Bali, Cakici & Whitelaw (NBER w14804)](https://causalclaims.trfetzer.com/paper/w14804.html),
  [overreaction critique (CFR)](https://cfr.ivo-welch.info/published/papers/gorman2021could.pdf),
  [QuantConnect implementation](https://www.quantconnect.com/research/20886/the-max-effect-with-vix-based-leverage-scaling/)
- LLM news sentiment: [Lopez-Lira & Tang (arXiv 2304.07619)](https://arxiv.org/abs/2304.07619v1)
- Factor momentum and banding: [Ehsani & Linnainmaa (NBER w25551)](https://img.nber.org/papers/w25551),
  [FAJ 2019 cost mitigation](https://rpc.cfainstitute.org/research/financial-analysts-journal/2019/ip-v4-n1-4-comparing-cost-mitigation-techniques)
- Customer momentum: [replication (arXiv 2301.11394)](https://arxiv.org/pdf/2301.11394)
- Days to cover: [Hong et al. summary](https://causalclaims.trfetzer.com/paper/w21166.html),
  [CXO Advisory](https://www.cxoadvisory.com/short-selling/days-to-cover-short-interest-as-a-stock-return-predictor)
- Dual momentum: [Advisor Perspectives](https://www.advisorperspectives.com/commentaries/2015/01/29/momentum-x-2-unleashing-the-true-power-of-momentum),
  [JSE replication](https://open.uct.ac.za/server/api/core/bitstreams/edf34055-a6af-4ea5-b880-2151ea6aaa9a/content)
- Halal ETFs: [ETF Central SPUS vs HLAL](https://www.etfcentral.com/compare-etfs/SPUS-vs-HLAL),
  [halal ETF analysis 2020–2025](https://www.halalterminal.com/research/halal-etf-analysis)
- Hierarchical risk parity: [Wikipedia](https://en.wikipedia.org/wiki/Hierarchical_Risk_Parity),
  [Brazilian out-of-sample study](https://periodicos.fgv.br/rbfin/article/download/89848/84802/201368)
- Frog-in-the-pan (Da, Gurun & Warachka 2014, RFS), short-term reversal (Jegadeesh 1990, Lehmann 1990), asset growth
  (Cooper, Gulen & Schill 2008), EAR (Brandt, Kishore, Santa-Clara & Venkatachalam 2008), multiple testing (Harvey, Liu
  & Zhu 2016; Bailey & López de Prado 2014), rebalance timing luck (Hoffstein, Faber & Braun 2020): cited from the
  literature, not fetched tonight.
