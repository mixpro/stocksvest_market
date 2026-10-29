# StocksVest App API: filters, automations and paper trading

Filters narrow the halal stock universe step by step, for example "price $10–100, up more than 3% in 5 days".
The app service stores filters, filter groups and the history of every run. It reads market data from the
[market API](API.md).

## Basics

- **Base URL:** `http://<server>:8100`. Interactive docs are at `/docs`, and the sample web app at `/ui/`.
- **API key:** send one of the `APP_API_KEYS` values (from the server's `.env`) in `X-API-Key`.
- **Errors:** JSON `detail`.
  - `401`: wrong key
  - `404`: not found
  - `409`: in use (can't delete)
  - `422`: invalid filter. `detail` is a message, or a list of `{loc, msg}` for field errors.
  - `502`: market API unreachable

```bash
curl -H "X-API-Key: $APP_KEY" http://localhost:8100/v1/filters
```

## How a filter works

```
inputs (union) ──► action 1 ──► action 2 ──► … ──► matching symbols
   883               342           88
```

1. **Inputs** are combined as a union:

   | Input | Meaning |
   |---|---|
   | `{"type": "UNIVERSE"}` | All halal symbols |
   | `{"type": "FILTER", "id": "flt_…"}` | The result of another filter, re-run live |
   | `{"type": "FILTER_GROUP", "id": "grp_…"}` | The union of a group's filters |
   | `{"type": "INVESTMENT_PORTFOLIO", "id": "pf_…"}` | Symbols held in a portfolio (bought by hand or by automations). Holdings that are no longer halal are included; they have no price, so price, sector and type actions drop them |
   | `WATCHLIST`, `WATCHLIST_GROUP` | Not available yet (`422`) |

2. **Actions** run top to bottom, and each one keeps only the symbols that match:

   | `type` | Fields | Keeps symbols where… |
   |---|---|---|
   | `SYMBOL_TYPE` | `assetTypes`: `COMMON_STOCK`, `ADR` | the asset type is in the list |
   | `SECTOR` | `sectorIds`: keys from market `GET /v1/sectors` | the sector is in the list |
   | `EXCLUDE_SYMBOLS` | `tickers`: list of tickers (case-insensitive) | the ticker is not in the list |
   | `SYMBOL_PRICE` | `priceReference`, `condition`, `value`, `maxValue` | the price matches |
   | `PRICE_CHANGE` | `periodAmount`, `periodUnit`, `valueType`, `condition`, `value`, `maxValue` | the change over the period matches |
   | `MARKET_CHANGE` | `periodAmount`, `periodUnit`, `condition`, `value`, `maxValue` | market regime gate: **every** symbol passes when the median % change of the whole universe over the period matches, **none** otherwise |
   | `SECTOR_RELATIVE_CHANGE` | `periodAmount`, `periodUnit`, `condition`, `value`, `maxValue` | the stock's % change minus the median % change of its sector (all halal stocks of it) matches; stocks without a sector are dropped |
   | `INDICATOR` | `indicator`: `VOLATILITY_20`, `ATR_14_PERCENT`, `PRICE_VS_SMA_50`, `PRICE_VS_SMA_200`, `FROM_52W_HIGH`, `ROE`, `NET_MARGIN`, `DEBT_TO_ASSETS`, `DAYS_SINCE_EARNINGS`, `DAYS_TO_EARNINGS`; `condition`, `value`, `maxValue` | the indicator (market `GET /v1/indicators`, completed daily bars) matches. E.g. `PRICE_VS_SMA_200` > 0 = above the 200-day average; `FROM_52W_HIGH` > −10 = within 10% of the 52-week high; `ROE` > 15 = profitable on its equity; `DAYS_TO_EARNINGS` > 7 = no earnings report expected within a week |
   | `AVERAGE_VOLUME` | `days` (default 20), `volumeType`, `condition`, `value`, `maxValue` | the average daily trading over the last `days` completed trading days matches |
   | `POSITION` | `portfolioIds`: one or more portfolio ids | the symbol is held in at least one of the portfolios (manual or automation shares) |
   | `UNREALIZED_PNL` | `portfolioIds`, `valueType`, `condition`, `value`, `maxValue` | the unrealized gain/loss of the shares held matches, e.g. `PERCENT` `LESS_THAN` `-8` = down more than 8% from the buy price (a stop-loss) |
   | `HOLDING_PERIOD` | `portfolioIds`, `periodUnit`, `condition`, `value`, `maxValue` | the time the symbol has been held matches, e.g. `DAYS` `MORE_THAN` `5` = held for more than 5 days |

   - **`priceReference`:**
     - `LATEST_BAR`: newest price: real-time for symbols a portfolio holds, else the newest minute bar (synced every minute)
     - `LAST_CLOSE`: close of the last *completed* trading day
   - **`periodUnit`:** `MINUTES` (max 7 days), `HOURS`, `DAYS`, `WEEKS`, `MONTHS`, `YEARS`. Only time the market is
     open counts: `DAYS` are trading days (on Monday, 2 days = Friday + Monday, compared with Thursday's close),
     `MINUTES` / `HOURS` are market time (04:00–20:00 New York). `WEEKS`, `MONTHS` and `YEARS` are calendar periods.
   - **`valueType`:** `PERCENT` (e.g. `3` = 3%) or `ABSOLUTE` (dollars)
   - **`condition`:**
     - `LESS_THAN`: x < value
     - `MORE_THAN`: x > value
     - `BETWEEN`: value ≤ x ≤ maxValue. `maxValue` is required, and only allowed with `BETWEEN`.
   - A symbol with no data for the comparison is dropped.
   - **`POSITION`:** the positions of several portfolios are combined as a union. Unknown portfolio ids are refused
     (`422`). Because actions only narrow what the inputs give, a held symbol that has left the halal universe
     doesn't appear with a `UNIVERSE` input. Example, "tech stocks I hold in either portfolio":
     `[{"type": "POSITION", "portfolioIds": ["pf_a", "pf_b"]}, {"type": "SECTOR", "sectorIds": ["TECHNOLOGY"]}]`
   - **`MARKET_CHANGE`:** the median of all ~880 stocks is the "typical stock", so a few big movers don't decide it.
     In a sell filter, `{"type": "MARKET_CHANGE", "periodAmount": 6, "periodUnit": "MONTHS", "condition": "LESS_THAN", "value": 0}`
     sells everything while the typical stock is down over 6 months.
   - **`AVERAGE_VOLUME`:** `volumeType` is `SHARES` (shares a day) or `DOLLARS` (shares × price a day). Days without
     a single trade count as 0, and today is left out until its session has ended. Use it to keep thinly traded
     stocks out: their price jumps on a few shares (a big "daily change" from one small trade) and their bid and ask
     are far apart. Example, "at least $1M a day over 20 days":
     `{"type": "AVERAGE_VOLUME", "days": 20, "volumeType": "DOLLARS", "condition": "MORE_THAN", "value": 1000000}`
   - **`HOLDING_PERIOD`:** counted from the oldest open lot of the symbol in the listed portfolios (manual and automation
     shares), i.e. when the position was opened; a later buy doesn't reset it. Calendar time: `DAYS` are 24 hours (a
     Friday buy is 3 days old on Monday), `MONTHS` 30.4375 days, `YEARS` 365.25 days. Symbols not held there are dropped.
     Typical uses in a sell filter: a time exit ("held more than 2 weeks"), or giving a new position room ("price change
     below −2.5% in 3 hours **and** held more than 1 day").
   - **`UNREALIZED_PNL`:** the shares of all the listed portfolios (manual and automation) count together, valued at
     the IBKR live last trade (else the newest minute bar, like the portfolio page) against what they cost. `PERCENT` is of the cost, `ABSOLUTE` in dollars.
     Symbols not held there, or without a price, are dropped. Unlike `PRICE_CHANGE`, which looks at a time window,
     this compares with the buy price. Example, "sell what is down 8% or more":
     `{"type": "UNREALIZED_PNL", "portfolioIds": ["pf_a"], "valueType": "PERCENT", "condition": "LESS_THAN", "value": -8}`

`GET /v1/filter-actions` returns all of this as data (fields, options, required rules), so a client can build its editor from it.

---

## Filters

### Create

```bash
curl -X POST -H "X-API-Key: $APP_KEY" -H "Content-Type: application/json" \
  http://localhost:8100/v1/filters -d '{
    "name": "Cheap momentum",
    "description": "",
    "isActive": true,
    "inputs": [{"type": "UNIVERSE"}],
    "actions": [
      {"type": "SYMBOL_PRICE", "priceReference": "LATEST_BAR", "condition": "BETWEEN", "value": 10, "maxValue": 100},
      {"type": "PRICE_CHANGE", "periodAmount": 5, "periodUnit": "DAYS", "valueType": "PERCENT", "condition": "MORE_THAN", "value": 3}
    ]
  }'
```

This returns `201` with the filter, plus `id`, `createdAt` and `updatedAt`.

### Read, update, delete

| Call | Notes |
|---|---|
| `GET /v1/filters` | All filters |
| `GET /v1/filters/{id}` | One filter |
| `PUT /v1/filters/{id}` | Replace with a full filter body (same as create) |
| `DELETE /v1/filters/{id}` | `204`. `409` if another filter or a group uses it |

When you save a filter, missing references and cycles (a filter that would use itself) are rejected with `422`.

### Preview (nothing stored)

`POST /v1/filters/preview` takes a filter body, so you can try it without saving:

```json
{
  "steps": [
    {"step": "INPUTS", "count": 881},
    {"step": "SYMBOL_PRICE", "count": 342},
    {"step": "PRICE_CHANGE", "count": 88}
  ],
  "symbols": [
    {"ticker": "ACIW", "id": "…", "name": "ACI Worldwide, Inc. - Common Stock", "sector": null,
     "price": 52.55, "priceTime": "2026-10-16T09:08:00Z", "dailyChangePercent": 1.2}
  ]
}
```

### Run and store

`POST /v1/filters/{id}/run` runs the saved filter and stores it in the run history:

```json
{
  "id": "run_…",
  "kind": "FILTER",
  "subjectId": "flt_…",
  "ranAt": "2026-10-16T09:24:17Z",
  "durationMs": 83,
  "status": "SUCCEEDED",
  "error": null,
  "definition": { "...": "the filter as it was at run time" },
  "result": { "steps": ["…"], "symbols": ["… with prices at run time"] }
}
```

`GET /v1/filters/{id}/runs?limit=20` lists past runs, newest first, without the full results.

---

## Filter groups

A group is a named set of filters. Running it runs every member, and its result is the **union** of their results.

| Call | Body / notes |
|---|---|
| `POST /v1/filter-groups` | `{"name": "Daily scans", "description": "", "filterIds": ["flt_…", "flt_…"]}` |
| `GET /v1/filter-groups`, `GET /v1/filter-groups/{id}` | |
| `PUT /v1/filter-groups/{id}` | Full body. Replaces the members |
| `DELETE /v1/filter-groups/{id}` | `409` if a filter uses the group as input |
| `POST /v1/filter-groups/{id}/run` | Runs and stores. `result.filters` has each member's steps and count; `result.symbols` is the union |
| `GET /v1/filter-groups/{id}/runs` | Run history |

---

## Automations

An automation runs filters on a schedule and acts on the matched symbols. Actions: **Notify** (in-app inbox)
and **Place order** (paper trading, see below). Adding to watchlists comes later; the API rejects it with
`422 … not available yet`.

```bash
curl -X POST -H "X-API-Key: $APP_KEY" -H "Content-Type: application/json" \
  http://localhost:8100/v1/automations -d '{
    "name": "Momentum alert",
    "runTargets": [{"type": "FILTER", "id": "flt_…"}],
    "schedules": [{"type": "MARKET", "anchor": "REGULAR_OPEN", "offsetMinutes": 30}],
    "actions": [{"type": "NOTIFY", "title": "{name}: {count} matches", "message": "", "skipWhenNoMatches": true}]
  }'
```

- **`runTargets`:** filters or filter groups. The matched symbols are the union of their results.
- **`actions`:** run in order. In `NOTIFY` titles and messages, `{name}` is the automation's name and `{count}` the number of matches.
- **Saving:** every schedule gets a stable `id`, and every save raises `configVersion`.
- **New automations** start `ACTIVE`.

### Schedules

| `type` | Fields | Example |
|---|---|---|
| `MARKET` | `anchor` (`PRE_MARKET_OPEN`, `REGULAR_OPEN`, `REGULAR_CLOSE`, `AFTER_HOURS_CLOSE`), `offsetMinutes`, optional `repeatEveryMinutes` (≥ 1) and `repeatUntil` | `{"type": "MARKET", "anchor": "REGULAR_CLOSE", "offsetMinutes": -15}` = 15 min before the close |
| `DAILY` | `atTimes` | `{"type": "DAILY", "atTimes": ["09:45", "15:30"]}` |
| `WEEKLY` | `onDays`, `atTimes`, `everyNWeeks` | `{"type": "WEEKLY", "onDays": ["MON"], "atTimes": ["10:00"]}` |
| `INTERVAL` | `every` (≥ 1), `unit` (`MINUTE`, `HOUR`, `DAY`), optional `onDays`, `window` | `{"type": "INTERVAL", "every": 30, "unit": "MINUTE", "window": {"start": "09:30", "end": "16:00"}}` |
| `ONCE` | `at` (with time zone offset) | `{"type": "ONCE", "at": "2026-10-20T10:00:00-04:00"}` |

**`MARKET` schedules:**
- They follow the trading calendar: they run only on trading days, use that day's session times (early closes included), and always use New York time.
- Repeats stop *before* `repeatUntil` (default: the regular close).

**How often is useful:** runs can repeat every minute, but only live prices change that fast: `UNREALIZED_PNL`
and order prices use IBKR quotes. `PRICE_CHANGE`, `SYMBOL_PRICE` and the ranking keys use the market service's
bars, which are real-time and synced every minute, except for symbols a portfolio holds or has an
order open for: the scheduler asks the market service for real-time IBKR bars of those every minute, so sell filters on
your holdings (price change, latest price) work on current prices.

**All types also take:**
- **`timezone`:** default `America/New_York`, e.g. `Asia/Jerusalem`. `MARKET` ignores it.
- **`startAt`:** no runs before this time.
- **`end`:** `{"type": "UNTIL", "until": …}` or `{"type": "COUNT", "count": 10}`.
- **`exceptions`:** a list of dates with no runs.
- **`missedPolicy`:** what happens to times missed while the app was down:
  - `RUN_ONCE` (default): one catch-up run
  - `SKIP`: recorded as one `SKIPPED` / `MISSED` run
  - `RUN_ALL`: one run per missed time

**Rules:**
- Each scheduled time runs **at most once**, even after a crash or restart.
- New, edited and resumed schedules start counting from now, so nothing is replayed.

### Endpoints

| Call | Notes |
|---|---|
| `GET /v1/automations?status=ACTIVE` | List |
| `POST /v1/automations`, `GET` / `PUT` / `DELETE /v1/automations/{id}` | Manage. `DELETE` keeps the run history |
| `POST /v1/automations/{id}/pause`, `/resume` | Paused automations don't run on schedule |
| `POST /v1/automations/{id}/preview` | What a run would do now. Nothing is stored or sent |
| `POST /v1/automations/preview` | Same for an unsaved automation (body = automation) |
| `POST /v1/automations/{id}/run` | Run now and store the run (works when paused or switched off) |
| `GET /v1/automations/{id}/runs` | Run history |
| `GET /v1/automations/{id}/next-runs?count=5` | Upcoming run times per schedule |
| `POST /v1/automations/schedule-preview?count=5` | Upcoming times for schedules being edited: `{"schedules": [...]}` |
| `GET /v1/automations/{id}/events` | Change log: created, updated (what changed), paused, resumed |
| `GET` / `PUT /v1/automations/switch` | Global switch: `{"automationsEnabled": false, "reason": "…"}`. While off, scheduled runs are recorded as `SKIPPED` / `SWITCH_OFF`. `GET` also shows scheduler health |

### A run

`status` is `SUCCEEDED`, `PARTIAL` (an action failed), `FAILED` (e.g. market API down) or `SKIPPED`
(`OVERLAP`, `PAUSED`, `SWITCH_OFF`, `MISSED`). `result` holds:

| Field | Contents |
|---|---|
| `trigger` | `SCHEDULE` (with `scheduleId`, `scheduledFor`) or `MANUAL` |
| `configVersion` | The automation version that ran (the full definition is in `definition`) |
| `market` | Session (`PRE_MARKET`, `REGULAR`, `AFTER_HOURS`, `CLOSED`) and how old the market data was |
| `targets` | Each filter's steps and count (groups: each member) |
| `matched` | Matched symbols with prices at run time |
| `actions` | Each action's status; `notificationId` for notifications sent |
| `metrics` | Duration and counts |

### Place order action

```json
{
  "type": "PLACE_ORDER", "side": "BUY", "orderType": "LIMIT", "limitOffsetPercent": -0.5, "timeInForce": "DAY",
  "selection": {"mode": "TOP_N", "topN": 3, "rankBy": "DAILY_CHANGE_PERCENT", "descending": true},
  "sizings": [{"mode": "CASH_AMOUNT", "value": 500}, {"mode": "CASH_AMOUNT", "value": 300}, {"mode": "CASH_AMOUNT", "value": 200}]
}
```

| Field | Values |
|---|---|
| `side` | `BUY`, or `SELL` (only shares bought with the same budget, by any of its automations; no short selling) |
| `orderType` | `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT`, `TRAILING_STOP` |
| `limitOffsetPercent` / `stopOffsetPercent` | Price = latest price × (1 + offset%), e.g. `-0.5` = 0.5% below |
| `trailPercent` | For `TRAILING_STOP` |
| `timeInForce` | `DAY`, `GTC`, `IOC`, `FOK`, `OPG`, `CLS` (whatever the broker supports) |
| `extendedHours` | Only with `LIMIT` + `DAY` |
| `selection` | `ALL_MATCHES`, or `TOP_N` with `topN` and `rankBy`: `DAILY_CHANGE_PERCENT`, `LAST_PRICE`, `DAILY_VOLUME`, `POSITION_VALUE`, `UNREALIZED_PNL_PERCENT`, `MOMENTUM_12_1`, `MOMENTUM_6_1` (% change from 12 or 6 months ago to 1 month ago; the latest month is left out because it tends to reverse), `AVERAGE_DOLLAR_VOLUME_20` (dollars traded a day over the last 20 trading days: the most-traded, usually largest, companies), `VOLATILITY_20` (annualised 20-day volatility; `descending: false` = calmest first), `FROM_52W_HIGH` (% below the 52-week high; descending = nearest the high). A `TOP_N` buy with `skipBuyIfHeld` leaves out what the budget holds or has a buy open for **before** taking the top N ("top 3" = the 3 best it doesn't own yet); they are listed as `alreadyHeld` in the run |
| `sizings` | One sizing for every symbol, or (`TOP_N`) one per rank. Buy: `QUANTITY`, `CASH_AMOUNT`, `PERCENT_OF_BUDGET`. Sell: `QUANTITY`, `PERCENT_OF_POSITION` |

**Rules:**
- Order actions trade with the **budgets** the automation is in (see [Budgets](#budgets)). Portfolios choose their
  automations; an automation in no budget runs its filters and other actions, and its order actions are `SKIPPED`.
- An automation in several budgets (also of different portfolios) plans and places orders **separately for each
  budget**, with that budget's money and shares. Filters run once per run.
- `missedPolicy: RUN_ALL` isn't allowed for these automations.
- Orders use whole shares.

`orderGuards` on the automation (defaults shown). They apply to each budget separately: `maxOrdersPerRun` is per
budget, and position caps and `skipBuyIfHeld` look at the budget's shares and open orders:

```json
{"maxOrdersPerRun": 10, "maxPositionValue": null, "maxPositionPercent": null,
 "regularHoursOnly": true, "skipBuyIfHeld": true, "maxDataAgeMinutes": 30, "requireLivePrice": false,
 "maxSpreadPercent": null, "rebuyCooldownMinutes": null, "maxSectorPercent": null,
 "targetVolatilityPercent": null, "maxOpenPositions": null, "maxCorrelation": null, "fractionalShares": false}
```

`requireLivePrice: true` places an order only when its symbol has a live quote (see below); otherwise it is skipped
(`NO_LIVE_PRICE`) instead of priced at the newest minute bar.

`maxSpreadPercent` skips a **buy** (`WIDE_SPREAD`) when its live quote's ask is more than this % above the bid
(measured from the midpoint). Thinly traded stocks can quote e.g. bid $25.00 / ask $28.50 (13%): a market buy fills
at the ask and starts that far below its value. Symbols without a live quote aren't checked, and sells are never
held back. The check runs after the top N is chosen, so a skipped symbol leaves its rank unfilled.

`rebuyCooldownMinutes` stops a **buy** of a symbol the budget sold (by any of its automations) less than that many
minutes ago (`RECENTLY_SOLD`). Without it, a buy automation and a stop-loss sell automation in the same budget can
trade one stock back and forth, losing the spread every time. A `TOP_N` buy leaves these symbols out **before**
taking the top N (listed as `recentlySold` in the run), so the next best takes the place.

`maxSectorPercent` caps one sector's share of the budget's equity: what the budget holds in it plus this run's buys.
A `TOP_N` buy leaves out stocks whose sector would cross it **before** taking the top N (listed as `sectorFull`),
counting each planned buy at `maxPositionPercent`, so lower-ranked stocks of other sectors take the places. A buy
that would still cross it shrinks, or skips (`SECTOR_FULL`). Stocks without a sector aren't capped, and holdings
that grew past the cap are not sold. Example: 5% positions with a 30% cap hold at most 6 buys of one sector.

`targetVolatilityPercent` sizes buys by risk: each buy and its position caps are multiplied by target ÷ the stock's
20-day annualised volatility, kept within 0.5–1.5. With 40 and `maxPositionPercent` 5, a stock moving 20% a year
gets 7.5%, one moving 80% gets 2.5%. Stocks without enough history are not scaled.

`maxCorrelation` (0–1) diversifies: a `TOP_N` buy leaves out a stock whose daily returns over the last 120 trading days
correlate above it with a stock the budget holds or one this run already picked (listed as `tooCorrelated`), so the
next ranked stock takes the place. Market `GET /v1/returns` supplies the returns.

`maxOpenPositions` limits how many symbols the budget holds at once (holdings, open buys and this run's buys). A buy
of a new symbol beyond it skips (`MAX_POSITIONS`); adding to a held symbol is not limited.

`fractionalShares: true` sizes `MARKET` + `DAY` orders to 0.0001 share instead of rounding down to whole shares, so a
$9,000 budget can hold 5% of a $1,200 stock (0.37 share). Other order types stay whole. Selling a whole position
always uses its exact quantity, so fractional holdings sell with or without it. Manual orders take fractions for
`MARKET` + `DAY` too.

**How a run plans orders:**
1. **Sync:** current fills and balances are imported from each budget's broker account first. If a broker can't
   be reached, that budget is reported as failed and the others still trade (the run is `PARTIAL`).
2. **Rank and size:** symbols are ranked and sized.
3. **Live prices:** for exactly the selected symbols (all matches, or the top N; sells only held ones), live quotes are
   fetched once per run (IBKR, through the market service's `/v1/quotes`) and used to price and size them: **buys at
   the ask, sells at the bid**. Nothing is fetched when the market is closed or the data too old (nothing can be
   placed then). Symbols without a live quote, or when live quotes fail, use the newest minute bar. Each intent
   has `priceSource` (`ALPACA` or `IBKR` = live, or `DELAYED`) and `quoteAgeSeconds`.
4. **Guards:** applied in rank order. Each symbol becomes an intent: `PLACE`, or `SKIP` with a reason:

   | `skipReason` | Meaning |
   |---|---|
   | `STALE_DATA` | Market data older than `maxDataAgeMinutes` |
   | `OUTSIDE_REGULAR_HOURS` | `regularHoursOnly` is on and the market isn't in its regular session |
   | `ALREADY_HELD` | `skipBuyIfHeld` is on and the budget holds it or has a buy open (from any of its automations) |
   | `MAX_POSITION` | `maxPositionValue` or `maxPositionPercent` reached (a smaller order is placed when it fits) |
   | `INSUFFICIENT_BUDGET` | Not enough budget left; the top ranks are served first |
   | `MAX_ORDERS_REACHED` | `maxOrdersPerRun` reached |
   | `NO_PRICE` | No price for the symbol |
   | `NO_LIVE_PRICE` | `requireLivePrice` is on and the symbol has no live quote |
   | `WIDE_SPREAD` | A buy whose live bid/ask spread is over `maxSpreadPercent` |
   | `RECENTLY_SOLD` | A buy of a symbol the budget sold within `rebuyCooldownMinutes` |
   | `SECTOR_FULL` | A buy whose sector is already at `maxSectorPercent` of the budget's equity |
   | `MAX_POSITIONS` | A buy of a new symbol when the budget already holds `maxOpenPositions` symbols |
   | `ZERO_QUANTITY` | The sizing comes to less than one share |
   | `NOT_SUPPORTED_BY_BROKER` | The broker doesn't support this order type, time in force or extended hours |

5. **Submit:** `PLACE` intents go to the broker with the client order ID `<runId>-<actionIndex>-<budgetId>-<ticker>`,
   so a retry never creates a second order.

**What the run records:** `result.budgets` (each budget's money `before` and `after`, or its `error`), and per
order action `budgets: [{budgetId, name, status, notHeld, intents}]`, every intent with its `orderId`, and the
order totals in `metrics` (including `budgetCount`). `result.liveQuotes` lists the live quotes the run used
(`asked`, `live`, `error`, and per symbol `source`, `bid`, `ask`, `last`, `time`, `ageSeconds`).

**Preview** shows the same plan without placing anything.

### What an automation did

`GET /v1/automations/{id}/record`: per budget it traded with, and in total: `ordersPlaced`, `openOrders`,
`buyFills` and `spent`, `sellFills` and `received`, `realizedPnL` of its sells, and `fees`. It counts only this
automation's own orders, so it still works after it leaves a budget, the budget is deleted, or the automation is
deleted. The budget's money and value over time are on the budget itself.

The automation's `budgets` field lists the budgets it is in: `[{id, name, portfolioId}]`. Its change log
(`/events`) records `BUDGET_ADDED` and `BUDGET_REMOVED`.

### Notifications

| Call | Notes |
|---|---|
| `GET /v1/notifications?unreadOnly=true&limit=50` | `{"unread": 3, "notifications": [...]}`, newest first. Each has `runId` |
| `POST /v1/notifications/{id}/read` | Mark one read |
| `POST /v1/notifications/read-all` | Mark all read |

---

## Paper trading

### Broker accounts

| Call | Notes |
|---|---|
| `POST /v1/broker-accounts` | `{"name": "Alpaca paper", "provider": "ALPACA", "credentialsRef": "ALPACA"}`. Keys come from `.env` as `<credentialsRef>_KEY` / `_SECRET` and are never stored. Tests the connection |
| `GET /v1/broker-accounts`, `GET /v1/broker-accounts/{id}` | `GET {id}` adds live balances, the broker's capabilities, its portfolios, and its **unassigned** cash and shares (not in any portfolio) |
| `GET /v1/broker-accounts/{id}/equity-history?period=1M&timeframe=1D` | The whole account's equity as reported by the broker, including history from before the app recorded anything. `period`: `1W`, `1M`, `3M`, `1A`, … `timeframe`: `1Min`, `5Min`, `15Min`, `1H`, `1D` |
| `DELETE /v1/broker-accounts/{id}` | `409` while a portfolio uses it |

Only **PAPER** accounts are accepted. IBKR comes later.

### Portfolios

A broker account can hold **several portfolios**, each with its own capital:

```
Alpaca paper account  $100,000
├─ Portfolio "Growth"      $40,000   ← capital
│   ├─ Budget "Momentum"     $10,000   ← used by "Buy gainers" and "Sell losers"
│   ├─ Budget "Dips"          $5,000   ← used by "Buy dips"
│   └─ unallocated           $25,000   ← manual orders
├─ Portfolio "Dividends"   $30,000
└─ unassigned              $30,000
```

- Each portfolio tracks its **own** cash, from its ledger: capital in or out, buy and sell fills, fees.
- Orders belong to one portfolio, so their fills land there.
- Trades made outside the app, in the broker's own UI, belong to no portfolio. They show up as the account's **unassigned** cash and shares.

| Call | Notes |
|---|---|
| `POST /v1/portfolios` | `{"name": "Growth", "brokerAccountId": "bka_…", "capital": 40000}`. `capital` comes from the account's unassigned cash |
| `GET /v1/portfolios/{id}` | Capital (`allocated`), `cash`, `unallocatedCash` (not in budgets), `positionsValue`, `equity`, unrealized and realized P&L, positions (per symbol, budget and buying automation; valued at live prices where available, see each position's `priceSource`), budgets, the account's unassigned cash, last sync |
| `POST /v1/portfolios/{id}/capital/allocate` / `withdraw` | `{"amount": 5000}`: move money between the account's unassigned cash and the portfolio. A withdrawal can only take unallocated cash |
| `GET /v1/portfolios/{id}/ledger` | Every change of the portfolio's cash, newest first |
| `GET /v1/portfolios/{id}/sales?year=2026` | CSV of every lot sold in the year: sell and buy time (UTC), shares, buy and sell price, cost, proceeds, gain (USD), budget. For tax records (fills imported from 2026-10-29 on) |
| `GET /v1/portfolios/{id}/dividends?year=2026` | Dividends credited in the year, per symbol: `received`, `withheld` (negative), `net`, plus totals. For purification and tax records |
| `DELETE /v1/portfolios/{id}` | Only when empty: no shares, no open orders, no budgets, and no filter using it (input or `POSITION` action). Its cash goes back to the account |
| `POST /v1/portfolios/{id}/sync` | Sync now. This also runs every minute in the background |
| `GET /v1/portfolios/{id}/orders?status=&automationId=&budgetId=&openOnly=true` | Orders, newest first. Each has `budgetId` (who pays) and `automationId` (who placed it) |
| `GET /v1/portfolios/{id}/fills`, `/lots` | Fills (sells with their `realizedPnL`), and position lots (`budgetId` owns them, `automationId` bought them) |
| `POST /v1/portfolios/{id}/orders` | Manual order (see below) |
| `GET /v1/orders/{id}` | One order with its fills |
| `POST /v1/orders/{id}/cancel` | Cancel an open order |
| `POST /v1/orders/kill-all` | Emergency stop, `{"confirm": "SELL ALL"}`: switches all scheduled automation runs off, cancels open buys and sells every share held in every portfolio (paper and live) at market (outside market hours the sells wait for the open). Shares not in a portfolio are left alone. Returns `cancelled`, `sells`, `errors`. The UI's **Kill all** button in the header calls it after a typed confirmation |

**Manual orders:**

```json
{"symbol": "AAPL", "side": "BUY", "type": "LIMIT", "quantity": 5, "limitPrice": 180, "timeInForce": "DAY"}
```

- Only **halal symbols** are accepted, except a `SELL` of a symbol that left the universe (market
  `GET /v1/symbols/removed`), so held shares can always be sold by hand.
- The order's `estimatedPrice` is the live ask (buy) or bid (sell) when a live quote is available.
- Without `budgetId`, a buy uses unallocated cash and a sell uses manual shares.
- With `budgetId` (a budget of this portfolio), a buy uses its money and a sell its shares.

**Order records:** each order keeps `estimatedPrice` and `averageFillPrice`, so `slippage` shows the difference.

**Who owns which shares:** shares are tracked in lots. A lot belongs to the budget that paid for it (or to the
portfolio's manual shares) and remembers which automation bought it. Sells close the budget's oldest lots first,
whoever bought them, so money and profit are exact per budget.

### Budgets

A portfolio can have **several budgets**. Each is a named pot of money carved out of the portfolio's cash.
The portfolio chooses which automations use each budget, and one automation can be in several budgets (also of
other portfolios). All automations in a budget **share** it: buys take its money, sells sell its shares and put
the money back. For example, a "buy gainers" and a "sell losers" automation in the same budget trade one pot.

| Call | Notes |
|---|---|
| `GET /v1/portfolios/{id}/budgets` | The portfolio's budgets with their automations |
| `POST /v1/portfolios/{id}/budgets` | `{"name": "Momentum", "amount": 10000, "automationIds": ["aut_…"]}`. `amount` comes from unallocated cash; names are unique per portfolio (`409`) |
| `GET /v1/budgets/{id}` | Money, automations, `positions` (with who bought them) and `openOrders` |
| `PUT /v1/budgets/{id}` | `{"name": "…"}` |
| `POST /v1/budgets/{id}/allocate` | `{"amount": 1000, "note": "…"}`, taken from the portfolio's unallocated cash |
| `POST /v1/budgets/{id}/withdraw` | Only the available part (not reserved, not invested) |
| `POST /v1/budgets/{id}/automations` | `{"automationId": "aut_…"}`: it trades with this budget from its next run |
| `DELETE /v1/budgets/{id}/automations/{automationId}` | It stops trading with it. Shares it bought stay in the budget; its open orders settle normally |
| `GET /v1/budgets/{id}/ledger` | Every change of its money, newest first, with the automation that caused it. Kept after deletion |
| `DELETE /v1/budgets/{id}` | `409` while it holds shares or has open orders. Its cash goes back to the portfolio |

**Budget fields:**
- **`cash`:** uninvested money, including the reserved part
- **`reserved`:** set aside for open buy orders
- **`available`:** `cash` − `reserved`; what new buys can use
- **`invested`:** what its shares cost
- **`allocated`**, **`realizedPnL`** and **`fees`**

**Ledger entry types:**
- `ALLOCATE`, `WITHDRAW`: you move money in or out
- `RESERVE`: a buy order is placed
- `RELEASE`: unused reservation returned when an order finishes or is cancelled
- `BUY_FILL`, `SELL_FILL`, `FEE`: from broker fills

Each entry has `cashAfter` and `reservedAfter`, and links to its order, fill, run and automation.

**Broker sync** (every minute, per broker account):
- The app imports balances, fills (each one once) and order statuses from the broker.
- Fills of orders placed outside the app are recorded as external, and their shares and cash count as unassigned.
- Account fees the broker books apart from the fills (e.g. Alpaca's daily **CAT fee**, a cent or so for the day's
  trades) are imported too. Each is split, in exact cents, between the budgets and manual trades that traded that
  day, by number of fills (a share for trades made outside the app stays unassigned). A fee for a day without app
  trades is split between the portfolios by their cash. They show as `FEE` entries in the portfolio and budget
  ledgers, with the broker's description.
- **Dividends** (Alpaca `DIV`, `DIVCGL`, `DIVCGS`, `DIVROC`, `DIVTXEX`) and the tax withheld from them (`DIVNRA`,
  `DIVFT`, `DIVTW`, `DIVFEE`) are imported too, from when the account was added. Each is split, in exact cents, by
  shares between the budgets and manual shares that held the stock: lots open on the pay date plus lots sold in the
  30 days before it (the broker gives no ex-date). Shares held outside the app keep their part unassigned. They show
  as `DIVIDEND` entries (negative = withheld) in the portfolio and budget ledgers. Alpaca paper pays no dividends.
- **Stocks no longer halal:** after each sync, during the regular session, shares of symbols removed as
  `NON_COMPLIANT` are sold at market (DAY), each budget and the manual shares selling their own, unless an open sell
  order already covers them. Other removal reasons (`PENDING`, `ERROR`, `NOT_IN_HALAL_DB`, `NOT_TRADABLE`) are not
  sold automatically. Every holding of a removed symbol is in a daily alert email while it is held.
- **Kill rule:** a portfolio `KILL_DRAWDOWN_PERCENT` (default 30, 0 = off) below its peak equity ÷ allocated (after-close
  snapshots) gets its buy automations paused, once per peak, with an email. See `docs/LIVE-RULES.md`.
- Differences with the broker are recorded in `lastSync.mismatches`, only where the portfolios hold more shares or cash than the broker.

### History

| Call | Notes |
|---|---|
| `GET /v1/portfolios/{id}/history?start=&end=&kind=` | Snapshots, oldest first, plus `current` (the same values right now). Each has `allocated`, `cash`, `unallocated`, `invested`, `positionsValue`, `equity`, `realizedPnL`, `unrealizedPnL` |
| `POST /v1/portfolios/{id}/snapshot` | Record one now (`MANUAL`) |
| `GET /v1/budgets/{id}/performance?start=&end=` | One point per trading day plus `current`. Each has `allocated`, `cash`, `reserved`, `invested`, `positionsValue`, `equity`, P&L, `fees`, `returnPercent` = (equity − allocated) / allocated, `openPositions`, and that day's `ordersPlaced` and `fills` |

**When portfolio snapshots are taken (`kind`):**
- `INTERVAL`: every 15 minutes during the regular session
- `CLOSE`: once, about 20 minutes after each close
- `MANUAL`: on request

Automation snapshots are taken after each close. Snapshots are kept permanently.

---

## Activity log

Everything that happens is recorded for the lifetime of the app, so problems can be traced.

**API changes:** every request that changes something (POST, PUT, PATCH, DELETE) is recorded, **including
failures**:
- the action, e.g. `FILTER_CREATE`, `AUTOMATION_PAUSE`, `PORTFOLIO_CAPITAL_ALLOCATE`, `ORDER_PLACE_MANUAL`
- the entity and related IDs
- the request body
- the status and error
- the duration
- the caller as `key:<fingerprint>` (never the key itself; `anonymous` without one)

**What the app does by itself:**

| Source | Actions |
|---|---|
| `SCHEDULER` | `AUTOMATION_RUN`, `AUTOMATION_RUNS_MISSED`, `SCHEDULE_ERROR`, `SCHEDULER_ERROR` |
| `SYNC` | `ORDER_SUBMITTED` / `ORDER_FILLED` / `ORDER_CANCELLED` / `ORDER_EXPIRED` / `ORDER_REJECTED` (status changes), `FILL_IMPORTED`, `EXTERNAL_FILL`, `BROKER_MISMATCH`, `BROKER_MISMATCH_RESOLVED`, `BROKER_SYNC_FAILED` |
| `API` / `SCHEDULER` | `ORDER_PLACED`, `ORDER_REJECTED`, `ORDER_SUBMIT_UNCERTAIN` (the order an API call or automation run placed) |
| `SYSTEM` | `APP_STARTED`, `DB_UPGRADE` |

**Linked entries:** entries caused by the same API request or scheduled run share a `correlationId`. Every
response includes it in the `X-Correlation-Id` header.

| Call | Notes |
|---|---|
| `GET /v1/activity?entityType=&entityId=&relatedId=&action=&source=&outcome=&correlationId=&since=&until=&limit=` | Newest first. `entityId` matches entries about that item **or** mentioning it. Page back with `beforeId=<nextBeforeId>` |
| `GET /v1/activity/actions` | Every action recorded so far, with counts |
| `GET /v1/activity/{id}` | One entry |

The **Activity** page in the sample app shows all of this, with filters, and lists everything from the same
request or run.

## Error answers

Every endpoint answers with JSON `{"detail": …}` and these codes:

| Code | Meaning |
|---|---|
| `401` | Missing or wrong key (every `/v1` endpoint) |
| `404` | Unknown ID. Run history and change logs still answer for **deleted** items, because their history is kept |
| `409` | In use, or would break consistency (e.g. delete an automation that holds shares) |
| `422` | Invalid input (`detail` is a message, or a list of `{loc, msg}` per field) |
| `502` | The market API or the broker is down, or answered something unexpected |

Money changes are serialized: concurrent requests queue up rather than both passing a balance check.

## Run history (for analysis)

Every stored run, of filters and automations now and of other actions later, is kept for **6 months**
(`RUN_RETENTION_DAYS`, default 183). Each run saves the definition and the prices at run time, so you can later
check how the selected symbols performed.

| Call | Notes |
|---|---|
| `GET /v1/runs?kind=FILTER&subjectId=flt_…&since=2026-10-01&limit=100&includeResult=true` | All params optional; newest first |
| `GET /v1/runs/{id}` | One run with definition and result |

Failed runs are stored too, with `status: "FAILED"` and `error`.

---

## Market data through the app

Clients only need an app key. The app calls the market API with its own key. Each endpoint maps to one market
endpoint, and parameters are the same as in the [market API](API.md):

| App endpoint | Market endpoint |
|---|---|
| `GET /v1/market/symbols?search=&exchange=&assetType=&sector=&limit=&offset=` | `GET /v1/symbols` |
| `GET /v1/market/symbols/{symbol}` | `GET /v1/symbols/{symbol}` |
| `GET /v1/market/sectors` | `GET /v1/sectors` |
| `GET /v1/market/bars/{symbol}?timeframe=&start=&end=&limit=&session=` | `GET /v1/bars/{symbol}` |
| `GET /v1/market/bars?symbols=A,B&timeframe=…` | `GET /v1/bars` |
| `GET /v1/market/snapshots?symbols=A,B` | `GET /v1/snapshots` |
| `GET /v1/market/price-change?periodAmount=&periodUnit=&symbols=` | `GET /v1/price-change` |
| `GET /v1/market/quotes?symbols=` | `GET /v1/quotes` (live prices) |
| `GET /v1/market/calendar?start=&end=` | `GET /v1/calendar` |
| `GET /v1/market/status` | `GET /v1/status` |

Market errors are passed on: `404` for an unknown symbol, and `502` if the market API is down.

```bash
curl -H "X-API-Key: $APP_KEY" "http://localhost:8100/v1/market/bars/AAPL?timeframe=1Day&limit=30"
```

---

## Sample app

Open `http://<server>:8100/ui/` and enter an app key, which is stored in your browser only. The **Filters**
page lets you:

- create, edit, reorder and delete filters and groups
- **Preview** while editing, and see how each step narrows the symbols
- **Run & store**, then open **History** to see past runs
- click a symbol to see its candlestick chart (1 min to 1 month bars)

The **Automations** page lets you build automations (filters, schedules, notify and place-order actions, order
guards), preview them (including the order plan per budget), see
upcoming run times, run now, pause or resume, see history and the change log, use the global switch, and read
the inbox. The bell in the header shows unread notifications.

The **Portfolio** page connects broker accounts, creates portfolios with their capital, and shows each
portfolio's capital, cash, equity and a history chart. The chart has 1W to All ranges, with an optional "whole
account" line from the broker. The page also shows positions (budget and buyer), the cash ledger, orders with
slippage, and fills, and can place and cancel manual paper orders. Under **Budgets** you create budgets, move money
in and out, choose which automations use each one, and open its ledger. Click a broker account to see its
portfolios, unassigned cash and shares, and its equity history. The Automations page shows the budgets an
automation is in and has a **Record** button with what it did per budget.

## Python example

```python
import requests

APP = "http://localhost:8100"
H = {"X-API-Key": "your-app-key"}

f = requests.post(f"{APP}/v1/filters", headers=H, json={
    "name": "Up 10% this month",
    "inputs": [{"type": "UNIVERSE"}],
    "actions": [{"type": "PRICE_CHANGE", "periodAmount": 1, "periodUnit": "MONTHS",
                 "valueType": "PERCENT", "condition": "MORE_THAN", "value": 10}],
}).json()

run = requests.post(f"{APP}/v1/filters/{f['id']}/run", headers=H).json()
for s in run["result"]["symbols"]:
    print(s["ticker"], s["price"])
```
