---
name: live-prices
description: StocksVest market data is real-time (paid Alpaca plan, no delay). Use before writing or changing anything that reads prices or quotes (watchers, alerts, order sizing, filters, charts, the phone app), and before telling the user how fresh any price, alert or signal is.
---

# Prices in StocksVest are live

The account pays for Alpaca's real-time data plan (Algo Trader Plus). There is **no data delay** anywhere in production.

## Facts

- The market service requests SIP bars with `SIP_DELAY_MINUTES=0` (the default; tinkergw doesn't override it) and
  syncs every minute from 04:00 to 20:00 New York, pre-market and after hours included.
- `GET /v1/status` on the market service reports `dataDelayMinutes: 0`.
- Live quotes: market `GET /v1/quotes` (`source: ALPACA`) are Alpaca real-time SIP snapshots: bid, ask and last trade
  right now. The app reaches them through `ctx.market.live_quotes(tickers)` (at most 100 tickers per call; it batches).
- Snapshots (`ctx.market.snapshots`, market `GET /v1/snapshots`) are the newest minute bar: at most about a minute old.
- In code, `priceSource: "DELAYED"` / `source: "DELAYED"` is an old label for "the newest minute bar, not a live
  quote". On this plan it is about a minute old, not 15–16 minutes.
- The 16-minute delay and `dataDelayMinutes: 16` exist only in the test fake (`tests/fake_market.py`). Never take
  them as production behaviour.

## Rules

1. **Never assume or add a delay.** No `- 16 minutes`, no "prices are 15 minutes late", no grace periods that exist
   to wait for delayed data.
2. **Use live prices where the moment matters**: a decision, an alert, an order. Take `ctx.market.live_quotes` for the
   symbols involved (last trade; bid for sells, ask for buys) and fall back to the snapshot only when there's no live
   quote. Snapshots are fine for scanning the whole universe (too many symbols for quotes), since they are at most a
   minute old.
3. **Extended hours count.** Prices exist from 04:00 to 20:00 ET; a check that matters for gaps (earnings, trial
   news) runs then, not only 09:30–16:00.
4. **Before saying how fresh something is, check production** instead of guessing, read-only over SSH:

   ```bash
   ssh saeed@tinkergw.local 'cd ~/stocksvest && docker compose exec -T market python -c "
   import json,urllib.request,os
   r=urllib.request.Request(\"http://localhost:8000/v1/status\",headers={\"X-API-Key\":os.environ[\"API_KEYS\"].split(\",\")[0]})
   d=json.load(urllib.request.urlopen(r)); print(d[\"dataDelayMinutes\"], d[\"lastIncrementalAt\"], d.get(\"lastError\"))"'
   ```

   A real lag comes from a failing sync (`lastError`, an old `lastIncrementalAt` during the session), not from the plan.
   Report that as a problem, not as normal.
5. **Tests** may use the fake's minute-bar fallback, but new code must work with a live quote present. Give the fake
   a `live` quote in the test when the code prefers live prices.
