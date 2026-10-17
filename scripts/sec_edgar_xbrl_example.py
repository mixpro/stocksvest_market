"""Side utility: example of reading company financials from the SEC EDGAR XBRL API.

Not used by the services. SEC requires a real contact in the User-Agent; replace the placeholder below.
Run: .venv/bin/python scripts/sec_edgar_xbrl_example.py
"""

import requests

HEADERS = {"User-Agent": "StocksVest your-email@example.com"}

# 1. Ticker -> CIK
data = requests.get("https://www.sec.gov/files/company_tickers_exchange.json", headers=HEADERS).json()
fields = data["fields"]  # ["cik", "name", "ticker", "exchange"]
rows = [dict(zip(fields, r)) for r in data["data"]]
cik = next(r["cik"] for r in rows if r["ticker"] == "AAPL")

# 2. All facts for one company
facts = requests.get(
    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=HEADERS
).json()

# Annual net income, one value per fiscal period
ni = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
annual = {x["frame"]: x["val"] for x in ni if x.get("frame", "").startswith("CY") and len(x["frame"]) == 6}
print(annual)

# 3. Screening: one concept for ALL companies in one call
frame = requests.get(
    "https://data.sec.gov/api/xbrl/frames/us-gaap/NetIncomeLoss/USD/CY2024.json", headers=HEADERS
).json()
for row in frame["data"][:5]:
    print(row["entityName"], row["cik"], row["val"])

