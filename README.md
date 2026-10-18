# stocksvest_market

Market data service for StocksVest: halal-compliant NYSE/NASDAQ stocks and their price history,
synced from Alpaca into SQLite and served over an HTTP API.

## What it stores

| Data | Window | Source |
|---|---|---|
| Symbols | Halal-compliant NYSE + NASDAQ common stocks and ADRs (no ETFs, warrants, units, rights, preferred) | NASDAQ symbol directory ∩ Alpaca tradable assets ∩ halal DB (`overall_status = COMPLIANT`) |
| Daily bars | 2 years | Alpaca, split- and dividend-adjusted |
| Hourly bars | 2 years, pre-market + regular + after-hours | Alpaca |
| Minute bars | Last 7 days, pre-market + regular + after-hours | Alpaca |
| Weekly / monthly / yearly bars | Built from daily bars on request | |
| Trading calendar | 2 years back, 90 days ahead | Alpaca |

## How it syncs

- **Every 5 minutes** from 04:00 to ~20:20 New York time on trading days: new minute, hourly and daily bars.
  On Alpaca's free plan the newest 15 minutes are not available, so data lags about 16 minutes
  (`SIP_DELAY_MINUTES`).
- **Nightly** (~20:45 New York time): refresh the calendar and symbol list, fill gaps, re-download
  symbols whose history was re-adjusted by a split or dividend, and drop data older than the windows above.
- **First start**: if the database is empty, the nightly job runs immediately and backfills everything.

## Setup

1. Alpaca: a Trading API account (paper is fine) and its API key and secret.
2. Copy `.env.example` to `.env` and fill in `ALPACA_KEY`, `ALPACA_SECRET`, `API_KEYS` and `ADMIN_KEY`.
   Generate keys with, for example, `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Run on a server with Docker

```bash
docker compose up -d --build
```

Then load the halal DB once (see below). Data lives in `./data` on the host.

### Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m stocksvest_market serve
```

Command-line jobs (they read `.env` from the current directory):

```bash
PYTHONPATH=src .venv/bin/python -m stocksvest_market import-halal ~/Projects/halalterminal_screen.db
PYTHONPATH=src .venv/bin/python -m stocksvest_market sync nightly
```

Tests: `.venv/bin/python -m pytest`

The app tests include:
- **A contract sweep over every endpoint** (`tests/test_app_contract.py`), built from the app's own OpenAPI:
  no key → 401, unknown IDs → 404, garbage bodies, broken JSON and bad query values never cause a 500.
- **End-to-end scenarios through the API** (`tests/test_app_scenarios.py`):
  - full setups and teardown in dependency order
  - deep cycle checks
  - state surviving restarts, and a scheduled time never running twice across them
  - randomized capital and budget operations with bookkeeping invariants (ledgers replay to balances)
  - concurrent requests that must not overspend
  - market and broker outages
  - odd input
  - every change appearing in the activity log

## App service (filters)

`docker compose up -d` also starts the **app service** on port 8100. It stores filters, filter groups,
automations (scheduled filter runs that notify and place paper orders), budgets, portfolios, orders and fills, an
inbox and a 6-month run history in `data/app.db`. It reads market data from the market service and trades through
broker adapters (Alpaca paper today), syncing with the broker every minute.

- Sample web app: `http://<server>:8100/ui/`. Enter one of the `APP_API_KEYS` values.
- API tutorial: [docs/APP_API.md](docs/APP_API.md)
- Settings in `.env`: `APP_API_KEYS`, `MARKET_API_KEY` (one of `API_KEYS`), and optionally `RUN_RETENTION_DAYS`

Code layout (`src/stocksvest_app/`): one folder per feature, each with its own models, storage and routes.

| Folder | Contents |
|---|---|
| `filters/` | Filter and group models, storage, evaluation engine, `/v1/filters*` routes |
| `automations/` | Automation and schedule models, schedule timing, storage, runner, order planner, budget pools, background scheduler, `/v1/automations*` and `/v1/notifications` routes |
| `brokers/` | Broker interface and normalized models, Alpaca adapter, broker accounts (`/v1/broker-accounts`) |
| `portfolios/` | Portfolios with their own capital and cash ledger (several per broker account), positions and lots per owner (`/v1/portfolios`) |
| `performance/` | Portfolio snapshots (every 15 min in the session and at the close), daily automation snapshots, broker equity history |
| `audit/` | Activity log: middleware recording every changing API request, plus entries for what the app does by itself (`/v1/activity`) |
| `orders/` | Orders and fills: placing, cancelling, broker sync (`/v1/orders`) |
| `runs/` | Shared run history (`/v1/runs`), used by every feature that runs something |
| `market/` | `client.py`: one function per market endpoint, all market URLs in `ENDPOINTS`. `data.py`: caching and batching helpers. `routes.py`: `/v1/market/*` |
| `api.py`, `deps.py`, `db.py`, `config.py` | App assembly, shared dependencies, DB connection and schema setup, settings |
| `static/` | Sample web app: Filters, Automations and Portfolio pages (`common.js` holds shared helpers) |

## Pulling the activity log

`scripts/pull_logs.py` downloads the app's activity log (standard library only; reads the URL and key from `.env`):

```bash
python3 scripts/pull_logs.py --since 2h --outcome FAILED
```

- **Filter by:** `--action`, `--source`, `--entity-type`, `--id` (about or mentioning an ID), or `--correlation` (everything one request or run caused).
- **Formats:** `--format table|json|jsonl|csv`, written to the screen or to `--out file`.
- **More:** `--all` for every matching entry, and `--follow` to keep printing new ones.

See `python3 scripts/pull_logs.py --help`.

## Viewing the data in Grafana

`docker compose up -d` also starts Grafana on port 3000 (user `admin`, password `GRAFANA_ADMIN_PASSWORD` from `.env`).
It reads the service's SQLite file directly, using the SQLite datasource plugin, and opens the **StocksVest Market** dashboard:

- Counts of symbols and stored bars, plus the last sync times
- A candlestick chart with volume for any symbol, with a Minute / Hour / Day timeframe picker. The dashboard time range picks the period.
- Latest daily change for every halal symbol
- Data coverage per symbol, to spot symbols with missing data
- History of halal DB imports

To write your own queries, open **Explore** and pick the *StocksVest Market* datasource. Tables:
`symbols`, `bars_1min`, `bars_1hour`, `bars_1day` (`ts` is unix seconds UTC; prices `o h l c`, volume `v`),
`calendar`, `sync_state`, `halal_imports`. Example:

```sql
SELECT strftime('%Y-%m-%dT%H:%M:%SZ', b.ts, 'unixepoch') AS time, b.c AS close
FROM bars_1day b JOIN symbols s ON s.id = b.symbol_id
WHERE s.ticker = 'AAPL' ORDER BY b.ts
```

(For time series, return the time as an ISO string in a column listed under the query's *Time formatted columns*.)

A second dashboard, **StocksVest Portfolios**, reads the app's database (`app.db`):
- portfolio capital, cash, equity and P&L over time
- each automation's equity and budget
- the latest daily result per automation
- recent orders and automation runs
- the cash ledger

Grafana opens the DBs read-only, so it cannot change your data.

> **Docker Desktop (macOS/Windows):** don't open `data/market.db` from the host, e.g. with the
> `sqlite3` CLI or a DB browser, while the containers are running. SQLite's WAL mode shares memory
> between processes, and that doesn't work across Docker Desktop's file sharing: the service starts
> failing with `disk I/O error` until it's restarted. Query it from inside a container instead,
> e.g. `docker compose exec market python -c "..."`, or use Grafana's Explore. On a Linux server,
> host access is fine.

## Updating the halal list

Upload a new halal DB (same `compliance` table format) with the admin key:

```bash
curl -X POST -H "X-API-Key: $ADMIN_KEY" -F "file=@halalterminal_screen.db" http://localhost:8000/admin/halal
```

- Newly compliant symbols are added and their history is backfilled in the background.
- Symbols that are no longer compliant are **deleted together with their price history**.
- The response lists `added` and `removed` tickers.
- As a safety net, an upload that would remove more than half of the symbols is refused; add `?force=true` to apply it anyway.
- If the file is invalid, nothing changes.

## API

Full tutorial with examples: [docs/API.md](docs/API.md).

Every `/v1` request needs the header `X-API-Key: <one of API_KEYS>`. Interactive docs: `http://<server>:8000/docs`.

Times are returned in UTC (`2026-10-13T14:30:00Z`). `start`/`end` accept a date (`2026-10-01`) or a datetime;
without a time zone they are read as New York time. A symbol can be given as its ticker or its `id`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness check (no key needed) |
| `GET /v1/status` | Symbol and bar counts, last sync times, last halal import, last error |
| `GET /v1/symbols?search=&exchange=&assetType=&limit=&offset=` | The halal universe |
| `GET /v1/symbols/{symbol}` | One symbol |
| `GET /v1/bars/{symbol}?timeframe=&start=&end=&limit=&session=` | Chart bars. `timeframe`: `1Min`, `1Hour`, `1Day`, `1Week`, `1Month`, `1Year`. `session=regular` drops pre/after-market bars |
| `GET /v1/bars?symbols=A,B&timeframe=...` | Bars for several symbols; `limit` is per symbol |
| `GET /v1/snapshots?symbols=A,B` | Latest price, latest daily bar, previous close, daily change and volume (all symbols if `symbols` is omitted) |
| `GET /v1/price-change?periodAmount=5&periodUnit=DAYS&symbols=A,B` | Change from `periodAmount × periodUnit` ago to the latest price. Units: `MINUTES` (up to 7 days), `HOURS`, `DAYS`, `WEEKS`, `MONTHS`, `YEARS` |
| `GET /v1/calendar?start=&end=` | Trading days with pre-market, regular and after-hours times |
| `POST /admin/halal` | Upload a new halal DB (admin key) |
| `POST /admin/sync?job=incremental\|nightly` | Run a sync now (admin key) |

### How the UML designs use these APIs

| Design (`UML/`) | API |
|---|---|
| `symbolId` everywhere | `id` from `/v1/symbols` (Alpaca asset id; stays the same if the ticker changes) |
| Filter input `UNIVERSE` | `/v1/symbols` |
| `SymbolTypeAction` (`AssetType`) | `assetType` on `/v1/symbols` |
| `SymbolPriceAction` `LAST_CLOSE` / `LATEST_BAR` | `dailyClose` / `latestPrice` from `/v1/snapshots` |
| `PriceChangeAction` | `/v1/price-change` (`changePercent` for `PERCENT`, `change` for `ABSOLUTE`) |
| `RankingKey` `LAST_PRICE`, `DAILY_CHANGE_PERCENT`, `DAILY_VOLUME` | `/v1/snapshots` |
| `Position.marketValue` / `unrealizedPnL`, order cost estimates | `latestPrice` from `/v1/snapshots` |
| `TradingCalendar` / `SessionTimes`, `OrderGuards.regularHoursOnly` | `/v1/calendar` |
| Symbol charts | `/v1/bars` |
| `SectorAction` | `sector` on `/v1/symbols`, keys from `/v1/sectors` (needs `SEC_USER_AGENT`) |
| Filters, groups, runs, automations | App service: [docs/APP_API.md](docs/APP_API.md) |

## Commit rules

The first line of every commit message must be:

```
[<part>][<type>] - <description>
```

| Field | Values |
|---|---|
| `part` | The part of the implementation, in lowercase words joined by `-`: `market`, `app-filters`, `app-automations`, … |
| `type` | `feature`, `fix`, `sub-task`, `refactor`, `docs`, `test`, `chore` |

Example: `[app-filters][fix] - Reject BETWEEN without maxValue`

The `.githooks/commit-msg` hook rejects commits that don't match. Merge, revert and `fixup!`/`squash!` messages that
git writes itself are allowed. Enable the hook once after cloning:

```bash
git config core.hooksPath .githooks
```
