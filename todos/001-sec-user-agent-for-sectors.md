# 001 · Set SEC contact so sectors get synced

**Status:** done (2026-10-17)
**Created:** 2026-10-16
**Area:** market service, sectors

## Problem

Sectors (used by the filters' `SECTOR` action and `GET /v1/sectors`) come from SEC EDGAR:
ticker → CIK → SIC code → sector. SEC requires a contact email in the User-Agent of every request;
without it `https://www.sec.gov/files/company_tickers_exchange.json` returns **403**.

Until this is set:
- Sector sync is skipped (a warning appears in the logs, and `GET /v1/status` shows `"sectorsEnabled": false`).
- Every symbol has `"sector": null`, so a `SECTOR` filter action matches nothing.

## To do

1. Add to `.env`:
   ```
   SEC_USER_AGENT=StocksVest-Market <contact-email>
   ```
2. Restart the market service: `docker compose up -d market`.
3. Run the nightly job, or wait for it (~20:45 New York time):
   ```bash
   curl -X POST -H "X-API-Key: $ADMIN_KEY" "http://localhost:8000/admin/sync?job=nightly"
   ```
   The first run looks up ~880 companies at ≤10 requests/second (about 2 minutes). Later runs only look up new symbols.
4. Check: `GET /v1/sectors` has counts and `unassigned` is near 0.

## Notes

- The SIC → sector mapping (`src/stocksvest_market/sectors.py`) approximates GICS sectors. Review the
  `OTHER` bucket after the first sync and adjust ranges if needed.
- Symbols without a SEC filing (some foreign ADRs) get `OTHER`.

## Resolution

`SEC_USER_AGENT` set in `.env` on 2026-10-17. The first sync assigned sectors to all 881 symbols:
Technology 302, Industrials 177, Healthcare 176, Consumer Discretionary 67, Basic Materials 54,
Consumer Staples 29, Financials 24, Energy 23, Real Estate 11, Utilities 9, Communication Services 7, Other 2.
