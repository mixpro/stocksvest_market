"""Sectors from SEC EDGAR: ticker -> CIK -> SIC code -> sector.

SEC requires a contact in the User-Agent (SEC_USER_AGENT, e.g. "StocksVest-Market you@example.com");
without it sector sync is skipped. The SIC -> sector mapping is an approximation of GICS sectors.
"""

import logging
import sqlite3
import time

import requests

from .config import Settings

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_MIN_INTERVAL = 0.15  # SEC allows 10 requests/second

SECTORS = {
    "BASIC_MATERIALS": "Basic Materials",
    "COMMUNICATION": "Communication Services",
    "CONSUMER_DISCRETIONARY": "Consumer Discretionary",
    "CONSUMER_STAPLES": "Consumer Staples",
    "ENERGY": "Energy",
    "FINANCIALS": "Financials",
    "HEALTHCARE": "Healthcare",
    "INDUSTRIALS": "Industrials",
    "REAL_ESTATE": "Real Estate",
    "TECHNOLOGY": "Technology",
    "UTILITIES": "Utilities",
    "OTHER": "Other",
}

# (first SIC, last SIC, sector). Checked in order, so specific ranges come before broad ones.
SIC_RANGES = [
    (3630, 3639, "CONSUMER_DISCRETIONARY"), (3711, 3716, "CONSUMER_DISCRETIONARY"),
    (3751, 3751, "CONSUMER_DISCRETIONARY"),
    (1311, 1389, "ENERGY"), (2900, 2999, "ENERGY"), (4610, 4619, "ENERGY"), (4922, 4925, "ENERGY"),
    (2830, 2836, "HEALTHCARE"), (3840, 3851, "HEALTHCARE"), (5047, 5047, "HEALTHCARE"),
    (5122, 5122, "HEALTHCARE"), (8000, 8099, "HEALTHCARE"), (8731, 8731, "HEALTHCARE"),
    (3570, 3579, "TECHNOLOGY"), (3600, 3699, "TECHNOLOGY"), (3810, 3839, "TECHNOLOGY"),
    (3861, 3861, "TECHNOLOGY"), (5045, 5045, "TECHNOLOGY"), (7370, 7379, "TECHNOLOGY"),
    (4800, 4899, "COMMUNICATION"), (2710, 2741, "COMMUNICATION"), (7810, 7841, "COMMUNICATION"),
    (7383, 7383, "COMMUNICATION"),
    (4900, 4921, "UTILITIES"), (4926, 4991, "UTILITIES"),
    (6500, 6553, "REAL_ESTATE"), (6798, 6798, "REAL_ESTATE"),
    (6000, 6799, "FINANCIALS"),
    (2000, 2199, "CONSUMER_STAPLES"), (2840, 2844, "CONSUMER_STAPLES"), (5140, 5159, "CONSUMER_STAPLES"),
    (5180, 5182, "CONSUMER_STAPLES"), (5400, 5499, "CONSUMER_STAPLES"), (5912, 5912, "CONSUMER_STAPLES"),
    (2200, 2399, "CONSUMER_DISCRETIONARY"), (2510, 2599, "CONSUMER_DISCRETIONARY"),
    (3100, 3199, "CONSUMER_DISCRETIONARY"),
    (3940, 3949, "CONSUMER_DISCRETIONARY"), (5000, 5099, "INDUSTRIALS"), (5200, 5999, "CONSUMER_DISCRETIONARY"),
    (7000, 7099, "CONSUMER_DISCRETIONARY"), (7200, 7299, "CONSUMER_DISCRETIONARY"),
    (7900, 7999, "CONSUMER_DISCRETIONARY"), (8200, 8299, "CONSUMER_DISCRETIONARY"),
    (1000, 1499, "BASIC_MATERIALS"), (2400, 2499, "BASIC_MATERIALS"), (2600, 2699, "BASIC_MATERIALS"),
    (2800, 2899, "BASIC_MATERIALS"), (3000, 3099, "BASIC_MATERIALS"), (3200, 3399, "BASIC_MATERIALS"),
    (1500, 1799, "INDUSTRIALS"), (3400, 3599, "INDUSTRIALS"), (3700, 3799, "INDUSTRIALS"),
    (3800, 3899, "INDUSTRIALS"), (3900, 3999, "INDUSTRIALS"), (4000, 4799, "INDUSTRIALS"),
    (5100, 5199, "INDUSTRIALS"), (7300, 7399, "INDUSTRIALS"), (8700, 8799, "INDUSTRIALS"),
    (100, 999, "CONSUMER_STAPLES"),
]


def sector_for_sic(sic: int | None) -> str:
    if sic is None:
        return "OTHER"
    for lo, hi, sector in SIC_RANGES:
        if lo <= sic <= hi:
            return sector
    return "OTHER"


class SecClient:
    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._last = 0.0

    def _get(self, url: str) -> dict:
        wait = self._last + SEC_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def cik_by_ticker(self) -> dict[str, int]:
        data = self._get(TICKERS_URL)
        idx = {name: i for i, name in enumerate(data["fields"])}
        # SEC writes share classes with a dash (BRK-B); we use a dot (BRK.B).
        return {row[idx["ticker"]].upper().replace("-", "."): int(row[idx["cik"]]) for row in data["data"]}

    def sic(self, cik: int) -> tuple[int | None, str | None]:
        data = self._get(SUBMISSIONS_URL.format(cik=cik))
        sic = data.get("sic")
        return (int(sic) if sic else None), (data.get("sicDescription") or None)


def sync_sectors(conn: sqlite3.Connection, sec: SecClient, refresh_all: bool = False) -> dict:
    """Fill sic / sector for symbols that don't have one yet (or all with refresh_all)."""
    where = "" if refresh_all else "WHERE sector IS NULL"
    todo = conn.execute(f"SELECT id, ticker FROM symbols {where}").fetchall()
    if not todo:
        return {"updated": 0, "unknown": 0}
    ciks = sec.cik_by_ticker()
    updated = unknown = 0
    for row in todo:
        cik = ciks.get(row["ticker"])
        sic, desc = (None, None)
        if cik is not None:
            try:
                sic, desc = sec.sic(cik)
            except requests.RequestException as e:
                log.warning("SEC lookup failed for %s: %s", row["ticker"], e)
                continue  # retried on the next sync
        if sic is None:
            unknown += 1
        conn.execute(
            "UPDATE symbols SET cik = ?, sic = ?, sic_description = ?, sector = ? WHERE id = ?",
            (cik, sic, desc, sector_for_sic(sic), row["id"]),
        )
        updated += 1
        if updated % 50 == 0:
            conn.commit()
    conn.commit()
    log.info("sectors: %d symbols updated, %d without a SIC code", updated, unknown)
    return {"updated": updated, "unknown": unknown}


def make_client(settings: Settings) -> SecClient | None:
    if not settings.sec_user_agent:
        log.warning("SEC_USER_AGENT is not set; skipping sector sync")
        return None
    return SecClient(settings.sec_user_agent)
