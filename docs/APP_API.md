# StocksVest App API: filters and automations

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

An automation runs filters on a schedule and acts on the matched symbols. Phase 1 has one action:
**Notify** (in-app inbox). Placing orders and adding to watchlists come in later phases; the API rejects
them with `422 … not available yet`.

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

### Notifications

| Call | Notes |
|---|---|
| `GET /v1/notifications?unreadOnly=true&limit=50` | `{"unread": 3, "notifications": [...]}`, newest first. Each has `runId` |
| `POST /v1/notifications/{id}/read` | Mark one read |
| `POST /v1/notifications/read-all` | Mark all read |

---

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

The **Automations** page lets you build automations (filters, schedules, notify actions), preview them, see
upcoming run times, run now, pause or resume, see history and the change log, use the global switch, and read
the inbox. The bell in the header shows unread notifications.

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
