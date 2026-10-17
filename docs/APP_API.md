# StocksVest App API: filters

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

## Run history (for analysis)

Every stored run, of filters now and of automations and other actions later, is kept for **6 months**
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

Open `http://<server>:8100/ui/` and enter an app key, which is stored in your browser only. You can:

- create, edit, reorder and delete filters and groups
- **Preview** while editing, and see how each step narrows the symbols
- **Run & store**, then open **History** to see past runs
- click a symbol to see its candlestick chart (1 min to 1 month bars)

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
