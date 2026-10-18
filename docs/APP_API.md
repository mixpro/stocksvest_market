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
   | `WATCHLIST`, `WATCHLIST_GROUP`, `INVESTMENT_PORTFOLIO` | Not available yet (`422`) |

2. **Actions** run top to bottom, and each one keeps only the symbols that match:

   | `type` | Fields | Keeps symbols where… |
   |---|---|---|
   | `SYMBOL_TYPE` | `assetTypes`: `COMMON_STOCK`, `ADR` | the asset type is in the list |
   | `SECTOR` | `sectorIds`: keys from market `GET /v1/sectors` | the sector is in the list |
   | `SYMBOL_PRICE` | `priceReference`, `condition`, `value`, `maxValue` | the price matches |
   | `PRICE_CHANGE` | `periodAmount`, `periodUnit`, `valueType`, `condition`, `value`, `maxValue` | the change over the period matches |

   - **`priceReference`:**
     - `LATEST_BAR`: newest price (~16 min delayed)
     - `LAST_CLOSE`: close of the last *completed* trading day
   - **`periodUnit`:** `MINUTES` (max 7 days), `HOURS`, `DAYS`, `WEEKS`, `MONTHS`, `YEARS`
   - **`valueType`:** `PERCENT` (e.g. `3` = 3%) or `ABSOLUTE` (dollars)
   - **`condition`:**
     - `LESS_THAN`: x < value
     - `MORE_THAN`: x > value
     - `BETWEEN`: value ≤ x ≤ maxValue. `maxValue` is required, and only allowed with `BETWEEN`.
   - A symbol with no data for the comparison is dropped.

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
| `MARKET` | `anchor` (`PRE_MARKET_OPEN`, `REGULAR_OPEN`, `REGULAR_CLOSE`, `AFTER_HOURS_CLOSE`), `offsetMinutes`, optional `repeatEveryMinutes` (≥ 5) and `repeatUntil` | `{"type": "MARKET", "anchor": "REGULAR_CLOSE", "offsetMinutes": -15}` = 15 min before the close |
| `DAILY` | `atTimes` | `{"type": "DAILY", "atTimes": ["09:45", "15:30"]}` |
| `WEEKLY` | `onDays`, `atTimes`, `everyNWeeks` | `{"type": "WEEKLY", "onDays": ["MON"], "atTimes": ["10:00"]}` |
| `INTERVAL` | `every` (minutes ≥ 5), `unit` (`MINUTE`, `HOUR`, `DAY`), optional `onDays`, `window` | `{"type": "INTERVAL", "every": 30, "unit": "MINUTE", "window": {"start": "09:30", "end": "16:00"}}` |
| `ONCE` | `at` (with time zone offset) | `{"type": "ONCE", "at": "2026-10-20T10:00:00-04:00"}` |

**`MARKET` schedules:**
- They follow the trading calendar: they run only on trading days, use that day's session times (early closes included), and always use New York time.
- Repeats stop *before* `repeatUntil` (default: the regular close).

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
| `side` | `BUY`, or `SELL` (only the automation's own shares; no short selling) |
| `orderType` | `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT`, `TRAILING_STOP` |
| `limitOffsetPercent` / `stopOffsetPercent` | Price = latest price × (1 + offset%), e.g. `-0.5` = 0.5% below |
| `trailPercent` | For `TRAILING_STOP` |
| `timeInForce` | `DAY`, `GTC`, `IOC`, `FOK`, `OPG`, `CLS` (whatever the broker supports) |
| `extendedHours` | Only with `LIMIT` + `DAY` |
| `selection` | `ALL_MATCHES`, or `TOP_N` with `topN` and `rankBy`: `DAILY_CHANGE_PERCENT`, `LAST_PRICE`, `DAILY_VOLUME`, `POSITION_VALUE`, `UNREALIZED_PNL_PERCENT` |
| `sizings` | One sizing for every symbol, or (`TOP_N`) one per rank. Buy: `QUANTITY`, `CASH_AMOUNT`, `PERCENT_OF_BUDGET`. Sell: `QUANTITY`, `PERCENT_OF_POSITION` |

**Rules:**
- An automation that places orders needs a `portfolioId` and a **budget**.
- `missedPolicy: RUN_ALL` isn't allowed for these automations.
- Orders use whole shares.

`orderGuards` on the automation (defaults shown):

```json
{"maxOrdersPerRun": 10, "maxPositionValue": null, "maxPositionPercent": null,
 "regularHoursOnly": true, "skipBuyIfHeld": true, "maxDataAgeMinutes": 30}
```

**How a run plans orders:**
1. **Sync:** current fills and balances are imported from the broker first.
2. **Rank and size:** symbols are ranked and sized.
3. **Guards:** applied in rank order. Each symbol becomes an intent: `PLACE`, or `SKIP` with a reason:

   | `skipReason` | Meaning |
   |---|---|
   | `STALE_DATA` | Market data older than `maxDataAgeMinutes` |
   | `OUTSIDE_REGULAR_HOURS` | `regularHoursOnly` is on and the market isn't in its regular session |
   | `ALREADY_HELD` | `skipBuyIfHeld` is on and the automation holds it or has a buy open |
   | `MAX_POSITION` | `maxPositionValue` or `maxPositionPercent` reached (a smaller order is placed when it fits) |
   | `INSUFFICIENT_BUDGET` | Not enough budget left; the top ranks are served first |
   | `MAX_ORDERS_REACHED` | `maxOrdersPerRun` reached |
   | `NO_PRICE` | No price for the symbol |
   | `ZERO_QUANTITY` | The sizing comes to less than one share |
   | `NOT_SUPPORTED_BY_BROKER` | The broker doesn't support this order type, time in force or extended hours |

4. **Submit:** `PLACE` intents go to the broker with the client order ID `<runId>-<actionIndex>-<ticker>`, so a retry never creates a second order.

**What the run records:** `budgetBefore` / `budgetAfter`, every intent with its `orderId`, and the order totals in `metrics`.

**Preview** shows the same plan without placing anything.

### Budget

| Call | Notes |
|---|---|
| `GET /v1/automations/{id}/budget` | `pool` and `entries` (the ledger, newest first) |
| `POST /v1/automations/{id}/budget/allocate` | `{"amount": 1000, "note": "…"}`, taken from the portfolio's unallocated cash |
| `POST /v1/automations/{id}/budget/withdraw` | Only the available part (not reserved, not invested) |

**`pool` fields:**
- **`cash`:** the automation's uninvested money, including the reserved part
- **`reserved`:** set aside for open buy orders
- **`available`:** `cash` − `reserved`; what new buys can use
- **`invested`:** what its shares cost
- **`realizedPnL`** and **`fees`**

**Ledger entry types:**
- `ALLOCATE`, `WITHDRAW`: you move money in or out
- `RESERVE`: a buy order is placed
- `RELEASE`: unused reservation returned when an order finishes or is cancelled
- `BUY_FILL`, `SELL_FILL`, `FEE`: from broker fills

Each entry has `cashAfter` and `reservedAfter`, and links to its order, fill and run. The ledger is kept permanently.

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
│   ├─ Automation A budget   $10,000
│   └─ Automation B budget    $5,000
├─ Portfolio "Dividends"   $30,000
└─ unassigned              $30,000
```

- Each portfolio tracks its **own** cash, from its ledger: capital in or out, buy and sell fills, fees.
- Orders belong to one portfolio, so their fills land there.
- Trades made outside the app, in the broker's own UI, belong to no portfolio. They show up as the account's **unassigned** cash and shares.

| Call | Notes |
|---|---|
| `POST /v1/portfolios` | `{"name": "Growth", "brokerAccountId": "bka_…", "capital": 40000}`. `capital` comes from the account's unassigned cash |
| `GET /v1/portfolios/{id}` | Capital (`allocated`), `cash`, `unallocatedCash` (not in automation budgets), `positionsValue`, `equity`, unrealized and realized P&L, positions by owner, budgets, the account's unassigned cash, last sync |
| `POST /v1/portfolios/{id}/capital/allocate` / `withdraw` | `{"amount": 5000}`: move money between the account's unassigned cash and the portfolio. A withdrawal can only take unallocated cash |
| `GET /v1/portfolios/{id}/ledger` | Every change of the portfolio's cash, newest first |
| `DELETE /v1/portfolios/{id}` | Only when empty: no shares, no open orders, no automations. Its cash goes back to the account |
| `POST /v1/portfolios/{id}/sync` | Sync now. This also runs every minute in the background |
| `GET /v1/portfolios/{id}/orders?status=&automationId=&openOnly=true` | Orders, newest first |
| `GET /v1/portfolios/{id}/fills`, `/lots` | Fills, and position lots (who owns which shares) |
| `POST /v1/portfolios/{id}/orders` | Manual order (see below) |
| `GET /v1/orders/{id}` | One order with its fills |
| `POST /v1/orders/{id}/cancel` | Cancel an open order |

**Manual orders:**

```json
{"symbol": "AAPL", "side": "BUY", "type": "LIMIT", "quantity": 5, "limitPrice": 180, "timeInForce": "DAY"}
```

- Only **halal symbols** are accepted.
- Without `automationId`, a buy uses unallocated cash and a sell uses manual shares.
- With `automationId`, the order uses that automation's budget or shares.

**Order records:** each order keeps `estimatedPrice` and `averageFillPrice`, so `slippage` shows the difference.

**Who owns which shares:** shares are tracked in lots per owner (an automation, or manual). Sells close the owner's oldest lots first, so profit and budgets are exact per automation.

**Broker sync** (every minute, per broker account):
- The app imports balances, fills (each one once) and order statuses from the broker.
- Fills of orders placed outside the app are recorded as external, and their shares and cash count as unassigned.
- Differences with the broker are recorded in `lastSync.mismatches`, only where the portfolios hold more shares or cash than the broker.

### History

| Call | Notes |
|---|---|
| `GET /v1/portfolios/{id}/history?start=&end=&kind=` | Snapshots, oldest first, plus `current` (the same values right now). Each has `allocated`, `cash`, `unallocated`, `invested`, `positionsValue`, `equity`, `realizedPnL`, `unrealizedPnL` |
| `POST /v1/portfolios/{id}/snapshot` | Record one now (`MANUAL`) |
| `GET /v1/automations/{id}/performance?start=&end=` | One point per trading day plus `current`. Each has the budget, `positionsValue`, `equity`, P&L, `fees`, `returnPercent` = (equity − budget) / budget, `openPositions`, and that day's `ordersPlaced` and `fills` |

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
| `SYSTEM` | `APP_STARTED`, `DB_UPGRADE`, `BUDGET_ORPHAN_RETURNED` |

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
guards, portfolio and budget), preview them (including the order plan), see
upcoming run times, run now, pause or resume, see history and the change log, use the global switch, and read
the inbox. The bell in the header shows unread notifications.

The **Portfolio** page connects broker accounts, creates portfolios with their capital, and shows each
portfolio's capital, cash, equity and a history chart. The chart has 1W to All ranges, with an optional "whole
account" line from the broker. The page also shows positions by owner, automation budgets, the cash ledger,
orders with slippage, and fills, and can place and cancel manual paper orders. Click a broker account to see its
portfolios, unassigned cash and shares, and its equity history. The Automations page has a **Performance** button
with an equity and return chart.

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
