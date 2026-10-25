# StocksVest Market API

Read price history and prices for halal-compliant NYSE/NASDAQ stocks.

## Basics

**Base URL:** `http://<server>:8000`. Interactive docs, where you can try every call, are at `http://<server>:8000/docs`.

**API key:** send it in the `X-API-Key` header on every `/v1` call. Read keys are the `API_KEYS` values in the server's `.env`.

```bash
curl -H "X-API-Key: $KEY" http://localhost:8000/v1/symbols/AAPL
```

**Symbols:** pass either the ticker (`AAPL`, `BRK.B`) or the symbol `id`. Only halal-compliant stocks exist in the API; any other ticker returns `404`.

**Times:**
- Responses are in UTC, e.g. `2026-10-15T14:32:00Z`.
- `start` and `end` accept a date (`2026-10-01`) or a datetime (`2026-10-01T09:30:00`). Without a time zone they are read as **New York time**.

**Data freshness:** bars come from Alpaca's SIP feed in real time (paid plan), synced every minute (`SYNC_INTERVAL_MINUTES`). On the free plan, which only serves data up to about 16 minutes ago, set `SIP_DELAY_MINUTES=16` and `ALPACA_RPM=180`.
Watched symbols (see [Live bars](#live-bars)) get real-time minutes from IBKR on top, so their latest price,
snapshot and price changes are current.

**Errors** return JSON with a `detail` field:

| Status | Meaning |
|---|---|
| `400` | Bad parameter (e.g. invalid date) |
| `401` | Missing or wrong API key |
| `404` | Unknown symbol, or not halal compliant |
| `409` | A sync is running (admin calls only); retry later |

```json
{"detail": "unknown symbol 'ZZZZ' (not listed, or not halal compliant)"}
```

---

## Symbols

### List symbols

`GET /v1/symbols`

| Param | Default | Description |
|---|---|---|
| `search` | | Ticker prefix or part of the name |
| `exchange` | | `NASDAQ` or `NYSE` |
| `assetType` | | `COMMON_STOCK` or `ADR` |
| `sector` | | Comma-separated sector keys from `/v1/sectors` |
| `limit` | 1000 | Max 50000 |
| `offset` | 0 | For paging |

```bash
curl -H "X-API-Key: $KEY" "http://localhost:8000/v1/symbols?search=AAP&limit=1"
```

```json
{
  "total": 1,
  "symbols": [
    {
      "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
      "ticker": "AAPL",
      "name": "Apple Inc. - Common Stock",
      "exchange": "NASDAQ",
      "assetType": "COMMON_STOCK",
      "sector": "TECHNOLOGY",
      "sic": 3571,
      "sicDescription": "Electronic Computers",
      "halalCompliant": true
    }
  ]
}
```

Store the `id`, not the ticker: the `id` stays the same if a company changes its ticker.

`sector`, `sic` and `sicDescription` come from SEC filings. They are `null` until sector sync is set up
(see `todos/001-sec-user-agent-for-sectors.md`).

### Sectors

`GET /v1/sectors` returns every sector key with its number of halal symbols:

```json
{"sectors": [{"key": "TECHNOLOGY", "name": "Technology", "symbols": 212}, "..."], "unassigned": 0}
```

Keys: `BASIC_MATERIALS`, `COMMUNICATION`, `CONSUMER_DISCRETIONARY`, `CONSUMER_STAPLES`, `ENERGY`, `FINANCIALS`,
`HEALTHCARE`, `INDUSTRIALS`, `REAL_ESTATE`, `TECHNOLOGY`, `UTILITIES`, `OTHER`.
They are approximate GICS sectors, mapped from SEC SIC codes.

### Get one symbol

`GET /v1/symbols/{symbol}` returns the same object as one item of the list above.

---

## Bars (chart data)

### One symbol

`GET /v1/bars/{symbol}`

| Param | Default | Description |
|---|---|---|
| `timeframe` | `1Day` | `1Min`, `1Hour`, `1Day`, `1Week`, `1Month`, `1Year` |
| `start` | oldest | Start of range |
| `end` | now | End of range |
| `limit` | 10000 | Max 50000. Returns the **most recent** `limit` bars in the range |
| `session` | `all` | `all` includes pre-market and after-hours; `regular` is 09:30–16:00 only (applies to `1Min` and `1Hour`) |

History available: `1Min` covers the last 7 days. `1Hour`, `1Day` and longer cover 2 years.

```bash
curl -H "X-API-Key: $KEY" "http://localhost:8000/v1/bars/AAPL?timeframe=1Day&limit=2"
```

```json
{
  "symbol": "AAPL",
  "timeframe": "1Day",
  "bars": [
    {"t": "2026-10-14T04:00:00Z", "o": 344.605, "h": 347.09, "l": 341.72, "c": 342.96, "v": 50215040, "n": 896603, "vw": 344.03361},
    {"t": "2026-10-15T04:00:00Z", "o": 346.95, "h": 347.85, "l": 344.25, "c": 344.62, "v": 11174599, "n": 274725, "vw": 345.926369}
  ]
}
```

| Field | Meaning |
|---|---|
| `t` | Bar start time (UTC). Daily bars start at midnight New York time |
| `o` `h` `l` `c` | Open, high, low, close |
| `v` | Volume (shares) |
| `n` | Number of trades |
| `vw` | Volume-weighted average price |

Notes:
- Prices are adjusted for splits and dividends, so charts have no artificial jumps.
- The latest bar of the current day, hour or week is still forming and updates on later calls.
- For `1Week`, `1Month` and `1Year`, `t` is the first trading day of the period.

### Several symbols

`GET /v1/bars?symbols=AAPL,MSFT&timeframe=1Hour&limit=1`

It takes the same parameters, plus `symbols` (comma-separated). `limit` applies per symbol (default 1000).

```json
{
  "timeframe": "1Hour",
  "bars": {
    "AAPL": [{"t": "2026-10-15T14:00:00Z", "o": 345.09, "h": 346.24, "l": 344.25, "c": 344.62, "v": 3929766, "n": 119786, "vw": 345.027727}],
    "MSFT": [{"t": "2026-10-15T14:00:00Z", "o": 534.735, "h": 537.01, "l": 532.55, "c": 536.83, "v": 2553015, "n": 81322, "vw": 534.893793}]
  }
}
```

---

## Prices

### Latest price and daily change

`GET /v1/snapshots?symbols=AAPL,MSFT`

Leave out `symbols` to get every symbol.

```json
{
  "snapshots": {
    "AAPL": {
      "latestPrice": 344.98,
      "latestTime": "2026-10-15T14:32:00Z",
      "dailyClose": 344.62,
      "dailyDate": "2026-10-15",
      "dailyVolume": 11174599,
      "previousClose": 342.96,
      "dailyChange": 2.02,
      "dailyChangePercent": 0.589
    }
  }
}
```

| Field | Meaning |
|---|---|
| `latestPrice` / `latestTime` | Close of the newest bar of any timeframe: use for current value and P&L |
| `dailyClose` / `dailyDate` | Close of the latest daily bar |
| `dailyVolume` | Volume of the latest day |
| `previousClose` | Close of the trading day before the latest price's day (on Monday, also in pre-market: Friday's) |
| `dailyChange` / `dailyChangePercent` | `latestPrice` vs `previousClose` |

### Price change over a period

`GET /v1/price-change?periodAmount=1&periodUnit=WEEKS&symbols=AAPL`

| Param | Description |
|---|---|
| `periodAmount` | Required, ≥ 1 |
| `periodUnit` | Required: `MINUTES` (up to 7 days), `HOURS`, `DAYS`, `WEEKS`, `MONTHS`, `YEARS` |
| `symbols` | Optional, comma-separated. All symbols if omitted |

```json
{
  "periodAmount": 1,
  "periodUnit": "WEEKS",
  "changes": {
    "AAPL": {
      "fromTime": "2026-10-08T04:00:00Z",
      "fromPrice": 340.42,
      "toTime": "2026-10-15T14:32:00Z",
      "toPrice": 344.98,
      "change": 4.56,
      "changePercent": 1.34
    }
  }
}
```

The period counts only time the market is open:

| `periodUnit` | Counts | Example |
|---|---|---|
| `DAYS` | Trading days (weekends and holidays skipped). The day of the latest price is day 1 | On Monday, `1` = Monday's move (from Friday's close), `2` = from Thursday's close |
| `MINUTES`, `HOURS` | Market time in the extended session (04:00–20:00 New York) | Monday 06:00, `3` hours = from Friday 19:00 |
| `WEEKS`, `MONTHS`, `YEARS` | Calendar time (a month is 30.4375 days, a year 365.25) | `1` week on Monday = from the Monday before |

`fromPrice` is the last price at or before the start of the period (`fromTime` is that bar). Values are `null` when
there is not enough history or trading calendar.

### Average daily trading

`GET /v1/volume?days=20&symbols=AAPL`

| Param | Description |
|---|---|
| `days` | Completed trading days to average, 1–260 (default 20). Today counts once its extended session has ended |
| `symbols` | Optional, comma-separated. All symbols if omitted |

```json
{
  "days": 20, "from": "2026-09-24", "to": "2026-10-21",
  "volumes": {"AAPL": {"averageVolume": 48210334.5, "averageDollarVolume": 16432112398.2, "daysTraded": 20}}
}
```

A trading day without any trade counts as 0, so `averageVolume` is the total over `days`, divided by `days`.

### Indicators

`GET /v1/indicators?symbols=AAPL` (all symbols if omitted)

From completed daily bars (today's counts once its extended session has ended); `null` without enough history.

```json
{"indicators": {"AAPL": {"close": 231.5, "volatility20": 24.1, "atr14Percent": 1.9, "sma50": 225.0, "sma200": 210.2,
  "priceVsSma50Percent": 2.9, "priceVsSma200Percent": 10.1, "high52w": 240.0, "fromHigh52wPercent": -3.5}}}
```

| Field | Meaning |
|---|---|
| `volatility20` | Annualised standard deviation of the last 20 daily log returns, % (21 bars needed) |
| `atr14Percent` | 14-day average true range as % of the close (15 bars) |
| `sma50`, `sma200` / `priceVsSma50Percent`, `priceVsSma200Percent` | Averages of the closes and the close's distance from them, % |
| `high52w` / `fromHigh52wPercent` | Highest high of the last 252 bars and the close vs it, % (0 = at the high) |

### Daily returns

`GET /v1/returns?days=120&symbols=AAPL,MSFT` (2–260 days, default 120; all symbols if omitted)

Close-to-close returns over the last `days` completed trading days: `days − 1` values per symbol, oldest first, `null`
where the stock has no bar on either day. For correlations between stocks.

```json
{"days": ["2026-05-07", "...", "2026-10-23"], "returns": {"AAPL": [0.0042, -0.0113, null, "..."]}}
```
`averageDollarVolume` uses each day's volume-weighted price (or its close). Values are `null` when the trading
calendar is shorter than `days`.

### Live quotes

`GET /v1/quotes?symbols=AAPL,JRSH` (up to 100 symbols): the price **right now**: real-time bid/ask and last trade
from Alpaca's SIP snapshots (`source: ALPACA`, paid data plan; used whenever Alpaca keys are set and IBKR is off), or
from Interactive Brokers when `IBKR_ENABLED=true` (`source: IBKR`). Without either, the newest minute bar
(`DELAYED`). A one-sided or crossed Alpaca quote keeps only the last trade (`bid`/`ask` null).

```json
{
  "quotes": {
    "AAPL": {"source": "IBKR", "bid": 340.35, "ask": 340.39, "last": 340.41, "price": 340.37,
             "time": "2026-10-21T15:28:34Z", "ageSeconds": 0},
    "JRSH": {"source": "DELAYED", "bid": null, "ask": null, "last": 6.26, "price": 6.26,
             "time": "2026-10-21T15:12:00Z", "ageSeconds": 1000}
  },
  "liveQuotes": {"enabled": true, "connected": true, "lastError": null, "lastQuoteAt": "2026-10-21T15:28:34Z"}
}
```

- `price` is the bid/ask midpoint, or the last trade when there is no bid/ask.
- A symbol falls back to `DELAYED` when IBKR has no live quote for it within a few seconds, the gateway is down, the
  account lacks the market data subscription, or another login of the same IBKR user holds the live data.
- The IBKR connection is read-only (it can't place orders) and uses its own client id. Settings: `IBKR_ENABLED`,
  `IBKR_HOST` (127.0.0.1), `IBKR_PORT` (4002 = paper login), `IBKR_CLIENT_ID` (31).
- `GET /v1/status` shows the same `liveQuotes` block.

### Live bars

`PUT /v1/live/watch` with `{"symbols": ["AAPL", "CSX"], "ttlMinutes": 10}`

Only with `IBKR_ENABLED=true` (off in production since 2026-10-25: the minute bars are real-time and synced every
minute, so it isn't needed). Streams IBKR real-time bars for these symbols (for example a portfolio's holdings) until `ttlMinutes` pass; send the
list again before then to keep them. The app does this every minute for everything its portfolios hold or have an order
open for.

- Each watched symbol first gets the last 30 minutes of 1-minute history from IBKR (covering any gap since the last sync), then
  5-second real-time bars, rolled up into minutes.
- While a symbol is watched, `latestPrice` in snapshots, the latest price of `GET /v1/price-change` and the start of
  `MINUTES` periods use these minutes, so `periodAmount=3&periodUnit=MINUTES` is the last 3 real minutes.
- Alpaca's full SIP bars still replace older minutes as usual; the live minutes are kept 2 days.
- At most `IBKR_LIVE_BARS_MAX` symbols stream at once (default 40; each takes one of the account's ~100 market data
  lines). Beyond that, the most recently renewed watches win and the rest are listed in `overLimit`.

```json
{"watching": ["AAPL", "CSX"], "overLimit": [], "unknown": [],
 "liveBars": {"enabled": true, "watching": 2, "streaming": 2, "overLimit": 0, "lastBarAt": "2026-10-26T14:31:05+00:00"}}
```

`GET /v1/live` returns the `liveBars` block alone; `GET /v1/status` includes it too.

---

## Trading calendar

`GET /v1/calendar?start=2026-11-26&end=2026-11-27`

The default range is today plus 30 days. Only trading days are returned, so a missing date means the market is closed (here, Thanksgiving on Nov 26).

```json
{
  "days": [
    {
      "date": "2026-11-27",
      "preMarketOpen": "2026-11-27T04:00:00-05:00",
      "regularOpen": "2026-11-27T09:30:00-05:00",
      "regularClose": "2026-11-27T13:00:00-05:00",
      "afterHoursClose": "2026-11-27T17:00:00-05:00",
      "isEarlyClose": true
    }
  ]
}
```

---

## Status

`GET /health` needs no key and returns `{"ok": true}`.

`GET /v1/status` returns sync health:

```json
{
  "symbols": 883,
  "bars": {"1Min": 1302573, "1Hour": 4318431, "1Day": 426080},
  "lastIncrementalAt": "2026-10-15T14:48:07+00:00",
  "lastNightlyAt": "2026-10-15T00:45:19+00:00",
  "lastHalalImport": {"id": 1, "imported_at": "2026-10-14T15:27:23+00:00", "compliant_count": 899, "added": 883, "removed": 0},
  "runningJob": null,
  "lastError": null,
  "dataDelayMinutes": 16
}
```

---

## Admin (admin key only)

These calls need the `ADMIN_KEY` value in `X-API-Key`.

### Upload a new halal DB

```bash
curl -X POST -H "X-API-Key: $ADMIN_KEY" -F "file=@halalterminal_screen.db" http://localhost:8000/admin/halal
```

```json
{"universe": 884, "added": ["NEWCO"], "removed": ["OLDCO"], "compliant_in_halal_db": 901, "backfillStarted": true}
```

- Symbols marked `COMPLIANT` are added, and their history downloads in the background.
- Symbols no longer compliant are **deleted with their history**.
- An invalid file returns `400` and changes nothing.
- An upload that would remove more than half the symbols is refused (`409`). Add `?force=true` to apply it anyway.

### Run a sync now

```bash
curl -X POST -H "X-API-Key: $ADMIN_KEY" "http://localhost:8000/admin/sync?job=incremental"
```

`job` is `incremental` (new bars) or `nightly` (symbols, calendar, gaps, split re-downloads). It runs in the background; watch `/v1/status`.

---

## Examples

### Python

```python
import requests

API = "http://localhost:8000"
HEADERS = {"X-API-Key": "your-key"}

# Daily chart for the last 6 months
bars = requests.get(f"{API}/v1/bars/AAPL", headers=HEADERS,
                    params={"timeframe": "1Day", "start": "2026-04-15"}).json()["bars"]

# Current prices for a watchlist
snaps = requests.get(f"{API}/v1/snapshots", headers=HEADERS,
                     params={"symbols": "AAPL,MSFT,NVDA"}).json()["snapshots"]
for ticker, s in snaps.items():
    print(ticker, s["latestPrice"], f'{s["dailyChangePercent"]:+.2f}%')

# Filter: halal stocks up more than 10% in a month
changes = requests.get(f"{API}/v1/price-change", headers=HEADERS,
                       params={"periodAmount": 1, "periodUnit": "MONTHS"}).json()["changes"]
winners = [t for t, c in changes.items() if c["changePercent"] and c["changePercent"] > 10]
```

### JavaScript

```js
const API = "http://localhost:8000";
const headers = { "X-API-Key": "your-key" };

const { bars } = await fetch(`${API}/v1/bars/AAPL?timeframe=1Hour&session=regular`, { headers })
  .then(r => r.json());
// bars[i] = { t, o, h, l, c, v, n, vw }: ready for a candlestick chart library
```

### Is the market open right now?

Call `/v1/calendar` for today. The market is open in the regular session if today is listed and the current time is between `regularOpen` and `regularClose`.
