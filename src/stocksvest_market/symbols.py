"""Build the symbol universe: NASDAQ/NYSE listings ∩ Alpaca tradable assets ∩ halal compliant."""

import csv
import io
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

OTHER_EXCHANGE_NAMES = {
    "N": "NYSE",
    "A": "NYSE American",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
    "M": "NYSE Texas",
}

# Preferred ($), warrants (.W/.WS), units (.U) and rights (.R) in the otherlisted ACT symbol format.
_NON_COMMON_TICKER = re.compile(r"\$|\.(W|WS|U|R|RT)$")
_NON_COMMON_NAME = re.compile(
    r"\b(warrants?|rights?|units?|preferred|notes?\s+due|debentures?|subordinated)\b", re.IGNORECASE
)

# Refuse to apply a symbol sync that would wipe out most of the universe (e.g. a bad download).
MAX_REMOVAL_FRACTION = 0.5


class SymbolSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Listing:
    ticker: str
    name: str
    exchange: str
    asset_type: str  # COMMON_STOCK | ADR


def classify(ticker: str, name: str) -> str | None:
    """Return the asset type, or None for warrants, units, rights, preferred shares and notes."""
    if _NON_COMMON_TICKER.search(ticker) or _NON_COMMON_NAME.search(name):
        return None
    return "ADR" if "depositary" in name.lower() else "COMMON_STOCK"


def _rows(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))


def parse_nasdaq_listed(text: str) -> list[Listing]:
    out = []
    for r in _rows(text):
        if r.get("Test Issue") != "N" or r.get("ETF") != "N":
            continue
        ticker, name = r["Symbol"].strip(), r["Security Name"].strip()
        asset_type = classify(ticker, name)
        if asset_type:
            out.append(Listing(ticker, name, "NASDAQ", asset_type))
    return out


def parse_other_listed(text: str, exchanges: list[str]) -> list[Listing]:
    out = []
    for r in _rows(text):
        if r.get("Test Issue") != "N" or r.get("ETF") != "N" or r.get("Exchange") not in exchanges:
            continue
        ticker, name = r["ACT Symbol"].strip(), r["Security Name"].strip()
        asset_type = classify(ticker, name)
        if asset_type:
            out.append(Listing(ticker, name, OTHER_EXCHANGE_NAMES.get(r["Exchange"], r["Exchange"]), asset_type))
    return out


def fetch_listings(exchanges: list[str]) -> list[Listing]:
    texts = []
    for url in (NASDAQ_LISTED_URL, OTHER_LISTED_URL):
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        texts.append(r.text)
    return parse_nasdaq_listed(texts[0]) + parse_other_listed(texts[1], exchanges)


def reconcile(
    conn: sqlite3.Connection,
    listings: list[Listing],
    alpaca_assets: list[dict],
    compliant: set[str],
    force: bool = False,
) -> dict:
    """Make the symbols table match the halal universe. Removed symbols are deleted with all their bars."""
    if len(listings) < 1000 or len(alpaca_assets) < 1000:
        raise SymbolSyncError(
            f"suspiciously small input ({len(listings)} listings, {len(alpaca_assets)} Alpaca assets); not applying"
        )
    assets = {a["symbol"]: a for a in alpaca_assets if a.get("tradable") and a.get("status") == "active"}
    universe = {
        assets[lst.ticker]["id"]: (lst, assets[lst.ticker])
        for lst in listings
        if lst.ticker in compliant and lst.ticker in assets
    }

    existing = {r["id"]: r["ticker"] for r in conn.execute("SELECT id, ticker FROM symbols")}
    removed = [sid for sid in existing if sid not in universe]
    added = [sid for sid in universe if sid not in existing]
    if existing and not force and len(removed) > MAX_REMOVAL_FRACTION * len(existing):
        raise SymbolSyncError(
            f"sync would remove {len(removed)} of {len(existing)} symbols; re-run with force to apply"
        )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.executemany("DELETE FROM symbols WHERE id = ?", [(sid,) for sid in removed])
        conn.executemany(
            """INSERT INTO symbols(id, ticker, name, exchange, asset_type, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET ticker = excluded.ticker, name = excluded.name,
                   exchange = excluded.exchange, asset_type = excluded.asset_type, updated_at = excluded.updated_at
               WHERE symbols.ticker != excluded.ticker OR symbols.name != excluded.name
                   OR symbols.exchange != excluded.exchange OR symbols.asset_type != excluded.asset_type""",
            [(sid, lst.ticker, lst.name, lst.exchange, lst.asset_type, now, now) for sid, (lst, _) in universe.items()],
        )
    summary = {
        "universe": len(universe),
        "added": sorted(universe[s][0].ticker for s in added),
        "removed": sorted(existing[s] for s in removed),
        "compliant_in_halal_db": len(compliant),
    }
    log.info("symbols: %d total, %d added, %d removed", len(universe), len(added), len(removed))
    return summary
