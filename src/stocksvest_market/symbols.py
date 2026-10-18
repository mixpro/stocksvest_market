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

# Exchange code in otherlisted.txt -> name stored on the symbol (symbols.exchange).
# Used by: parse_other_listed. Which codes are kept comes from Settings.other_exchanges (default N = NYSE).
OTHER_EXCHANGE_NAMES = {
    "N": "NYSE",
    "A": "NYSE American",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
    "M": "NYSE Texas",
}

# Preferred ($), warrants (.W/.WS), units (.U) and rights (.R) in the otherlisted ACT symbol format.
# Used by: classify (together with _NON_COMMON_NAME).
_NON_COMMON_TICKER = re.compile(r"\$|\.(W|WS|U|R|RT)$")
_NON_COMMON_NAME = re.compile(
    r"\b(warrants?|rights?|units?|preferred|notes?\s+due|debentures?|subordinated)\b", re.IGNORECASE
)

# Refuse to apply a symbol sync that would wipe out most of the universe (e.g. a bad download).
# Used by: reconcile (bypassed with force=True, e.g. POST /admin/halal?force=true).
MAX_REMOVAL_FRACTION = 0.5


class SymbolSyncError(RuntimeError):
    """A symbol sync was refused: suspiciously small downloads, or it would remove more than
    MAX_REMOVAL_FRACTION of the symbols without force.

    Used by: reconcile and sync.sync_symbols (raised), api.upload_halal (-> HTTP 409).
    """

    pass


@dataclass(frozen=True)
class Listing:
    """One security from the NASDAQ symbol directory that passed the filters (not a test issue, not an ETF,
    a common stock or ADR).

    Attributes:
        ticker: Alpaca/NASDAQ format, e.g. "BRK.B".
        name: security name from the directory.
        exchange: "NASDAQ", "NYSE", "NYSE American", ... (see OTHER_EXCHANGE_NAMES).
        asset_type: "COMMON_STOCK" or "ADR" (see classify).

    Used by: parse_nasdaq_listed / parse_other_listed (created), fetch_listings, reconcile.
    """

    ticker: str
    name: str
    exchange: str
    asset_type: str  # COMMON_STOCK | ADR


def classify(ticker: str, name: str) -> str | None:
    """Classify a listed security.

    Args:
        ticker: symbol from the directory; "$" (preferred) and .W/.WS/.U/.R/.RT suffixes mark non-common securities.
        name: security name; words like warrant, unit, right, preferred, notes due mark non-common securities.

    Returns:
        "ADR" (name mentions depositary), "COMMON_STOCK", or None for warrants, units, rights, preferred shares
        and notes, which are excluded.

    Used by: parse_nasdaq_listed, parse_other_listed.
    """
    if _NON_COMMON_TICKER.search(ticker) or _NON_COMMON_NAME.search(name):
        return None
    return "ADR" if "depositary" in name.lower() else "COMMON_STOCK"


def _rows(text: str) -> list[dict]:
    """Parse a pipe-separated NASDAQ directory file into dicts keyed by its header, skipping the
    "File Creation Time" footer.

    Args:
        text: the whole file.

    Used by: parse_nasdaq_listed, parse_other_listed.
    """
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))


def parse_nasdaq_listed(text: str) -> list[Listing]:
    """Common stocks and ADRs listed on NASDAQ.

    Args:
        text: contents of nasdaqlisted.txt.

    Returns:
        Listings with exchange "NASDAQ"; test issues, ETFs and non-common securities are dropped.

    Used by: fetch_listings.
    """
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
    """Common stocks and ADRs listed on other exchanges (NYSE, ...).

    Args:
        text: contents of otherlisted.txt.
        exchanges: exchange codes to keep, e.g. ["N"] for NYSE (Settings.other_exchanges).

    Returns:
        Listings named by OTHER_EXCHANGE_NAMES; test issues, ETFs and non-common securities are dropped.

    Used by: fetch_listings.
    """
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
    """Download both NASDAQ directory files and parse them.

    Args:
        exchanges: exchange codes kept from otherlisted.txt (see parse_other_listed).

    Returns:
        NASDAQ listings followed by the other exchanges' listings.

    Raises:
        requests.HTTPError: a download failed.

    Used by: sync.sync_symbols.
    """
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
    """Make the symbols table match the halal universe: listed AND tradable at Alpaca AND compliant.

    Symbols that leave the universe are deleted together with all their bars (foreign keys cascade).
    A symbol whose ticker changed keeps its id (the Alpaca asset id) and is updated in place.

    Args:
        conn: market DB connection.
        listings: from fetch_listings.
        alpaca_assets: from AlpacaClient.assets (only tradable, active ones count).
        compliant: tickers from halal.compliant_tickers.
        force: apply even if more than MAX_REMOVAL_FRACTION of the current symbols would be removed.

    Returns:
        {"universe": size, "added": [tickers], "removed": [tickers], "compliant_in_halal_db": n}.

    Raises:
        SymbolSyncError: fewer than 1000 listings or assets (a bad download), or too many removals without force.

    Used by: sync.sync_symbols.
    """
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
