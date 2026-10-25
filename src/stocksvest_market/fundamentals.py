"""Fundamentals and earnings dates from SEC EDGAR (same SEC_USER_AGENT as the sectors).

- Fundamentals come from XBRL "frames": one call returns one figure for every company for one period (net income and
  revenue per calendar year, equity and assets at each quarter end). Stored by CIK in `fundamentals`.
- Earnings dates are a company's 8-K filings with item 2.02 ("Results of Operations"), from its submissions. Stored
  by CIK in `earnings`.

A figure counts from its period end plus a filing lag (ANNUAL_LAG / QUARTER_LAG days), so a backtest never sees a
number before it was public. Used by: sync.run_nightly (sync), api.indicators and the backtester (as_of).
"""

import logging
import sqlite3
from datetime import date, timedelta

import requests

from .sectors import SUBMISSIONS_URL, SecClient

log = logging.getLogger(__name__)

FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{period}.json"
# Stored metric -> XBRL tags tried in order (companies report revenue under different tags). Used by: sync
ANNUAL = {"NET_INCOME": ["NetIncomeLoss"],
          "REVENUE": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]}
INSTANT = {"EQUITY": ["StockholdersEquity"], "ASSETS": ["Assets"]}
ANNUAL_LAG, QUARTER_LAG = 90, 60  # days after the period end until a figure counts (10-K / 10-Q deadlines)
NEXT_EARNINGS_DAYS = 91  # the next report is expected a quarter after the last one. Used by: as_of
FIRST_YEAR = 2015

SCHEMA = """
CREATE TABLE IF NOT EXISTS fundamentals (
    cik        INTEGER NOT NULL,
    metric     TEXT NOT NULL,      -- NET_INCOME, REVENUE (calendar-year frames), EQUITY, ASSETS (quarter ends)
    period_end TEXT NOT NULL,      -- YYYY-MM-DD
    value      REAL NOT NULL,
    PRIMARY KEY (cik, metric, period_end)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS earnings (
    cik  INTEGER NOT NULL,
    date TEXT NOT NULL,            -- 8-K item 2.02 filing date
    PRIMARY KEY (cik, date)
) WITHOUT ROWID;
"""


def _periods(today: date, since: int) -> list[tuple[str, bool]]:
    """(frame period, is annual) from `since` up to the last finished period: CY2023 and CY2023Q1I ... CY2023Q4I."""
    out = []
    for y in range(since, today.year + 1):
        if date(y + 1, 1, 1) <= today:
            out.append((f"CY{y}", True))
        out += [(f"CY{y}Q{q}I", False) for q in range(1, 5) if date(y, 3 * q, 28) < today]
    return out


def sync(conn: sqlite3.Connection, sec: SecClient, today: date | None = None, full: bool = False) -> dict:
    """Download the frames (the last two years, or all since FIRST_YEAR the first time / with `full`) and the
    earnings dates of symbols whose next report is due (or that have none yet).

    Returns:
        {"facts": rows written, "earnings": companies refreshed}.

    Used by: sync.run_nightly.
    """
    conn.executescript(SCHEMA)
    today = today or date.today()
    empty = conn.execute("SELECT 1 FROM fundamentals LIMIT 1").fetchone() is None
    since = FIRST_YEAR if full or empty else today.year - 2
    ciks = {r[0] for r in conn.execute("SELECT cik FROM symbols WHERE cik IS NOT NULL")}
    facts = 0
    for period, annual in _periods(today, since):
        for metric, tags in (ANNUAL if annual else INSTANT).items():
            seen = set()
            for tag in tags:
                try:
                    data = sec._get(FRAMES_URL.format(tag=tag, period=period))["data"]
                except requests.RequestException:
                    continue  # a tag without a frame for that period (404)
                rows = [(d["cik"], metric, d["end"], d["val"]) for d in data if d["cik"] in ciks and d["cik"] not in seen]
                seen |= {r[0] for r in rows}
                conn.executemany("INSERT OR REPLACE INTO fundamentals VALUES (?, ?, ?, ?)", rows)
                facts += len(rows)
        conn.commit()
    due = (today - timedelta(days=NEXT_EARNINGS_DAYS - 7)).isoformat()
    last = dict(conn.execute("SELECT cik, MAX(date) FROM earnings GROUP BY cik"))
    refreshed = 0
    for cik in sorted(c for c in ciks if last.get(c, "") < due):
        try:
            recent = sec._get(SUBMISSIONS_URL.format(cik=cik))["filings"]["recent"]
        except requests.RequestException as e:
            log.warning("SEC submissions failed for CIK %s: %s", cik, e)
            continue
        rows = [(cik, d) for f, d, it in zip(recent["form"], recent["filingDate"], recent["items"])
                if f == "8-K" and "2.02" in (it or "")]
        conn.executemany("INSERT OR IGNORE INTO earnings VALUES (?, ?)", rows)
        refreshed += 1
        if refreshed % 50 == 0:
            conn.commit()
    conn.commit()
    log.info("fundamentals: %d facts, earnings refreshed for %d companies", facts, refreshed)
    return {"facts": facts, "earnings": refreshed}


def load(conn: sqlite3.Connection) -> tuple[dict[int, dict[str, list]], dict[int, list[str]]]:
    """All facts ({cik: {metric: [(period_end, value)] sorted}}) and earnings dates ({cik: [date] sorted}); empty
    when the tables don't exist yet.

    Used by: api.indicators, backtest history.
    """
    facts: dict[int, dict[str, list]] = {}
    earnings: dict[int, list[str]] = {}
    try:
        for cik, metric, end, value in conn.execute("SELECT cik, metric, period_end, value FROM fundamentals "
                                                    "ORDER BY cik, metric, period_end"):
            facts.setdefault(cik, {}).setdefault(metric, []).append((end, value))
        for cik, d in conn.execute("SELECT cik, date FROM earnings ORDER BY cik, date"):
            earnings.setdefault(cik, []).append(d)
    except sqlite3.OperationalError:  # no such table: never synced
        pass
    return facts, earnings


def _latest(rows: list | None, day: str, lag: int) -> float | None:
    """The newest value whose period end + lag is on or before `day`."""
    for end, value in reversed(rows or []):
        if (date.fromisoformat(end) + timedelta(days=lag)).isoformat() <= day:
            return value
    return None


def as_of(facts: dict[str, list] | None, earnings: list[str] | None, day: str) -> dict:
    """A company's fundamentals as public on `day` (YYYY-MM-DD).

    Returns:
        {roe: last annual net income / latest equity in %, netMargin: annual net income / revenue in %,
        debtToAssets: (assets - equity) / assets in %, daysSinceEarnings: days since the last 8-K 2.02,
        daysToEarnings: days until the expected next one (last + NEXT_EARNINGS_DAYS; negative when overdue)};
        None where data is missing.
    """
    facts = facts or {}
    ni, rev = _latest(facts.get("NET_INCOME"), day, ANNUAL_LAG), _latest(facts.get("REVENUE"), day, ANNUAL_LAG)
    eq, assets = _latest(facts.get("EQUITY"), day, QUARTER_LAG), _latest(facts.get("ASSETS"), day, QUARTER_LAG)
    past = [d for d in earnings or [] if d <= day]
    since = (date.fromisoformat(day) - date.fromisoformat(past[-1])).days if past else None
    return {"roe": ni / eq * 100 if ni is not None and eq and eq > 0 else None,
            "netMargin": ni / rev * 100 if ni is not None and rev else None,
            "debtToAssets": (assets - eq) / assets * 100 if assets and eq is not None else None,
            "daysSinceEarnings": since,
            "daysToEarnings": NEXT_EARNINGS_DAYS - since if since is not None else None}


if __name__ == "__main__":  # self-check
    f = {"NET_INCOME": [("2023-12-31", 10.0)], "REVENUE": [("2023-12-31", 100.0)],
         "EQUITY": [("2023-12-31", 50.0), ("2024-03-31", 40.0)], "ASSETS": [("2023-12-31", 200.0)]}
    early = as_of(f, ["2024-01-25"], "2024-03-05")  # annual not public yet (lag 90), Q4 equity is (lag 60)
    assert early["roe"] is None and early["debtToAssets"] == 75.0 and early["daysSinceEarnings"] == 40
    late = as_of(f, ["2024-01-25", "2024-04-25"], "2024-06-01")
    assert late["roe"] == 25.0 and late["netMargin"] == 10.0 and late["daysToEarnings"] == 91 - 37
    assert as_of(None, None, "2024-01-01")["roe"] is None
    print("ok")
