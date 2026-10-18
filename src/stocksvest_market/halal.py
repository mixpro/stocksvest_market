"""Read the halal screening database. Only symbols with overall_status = COMPLIANT are kept."""

import sqlite3
from pathlib import Path


class HalalDbError(ValueError):
    """The file is not a usable halal screening database (not SQLite, or no compliance table/columns,
    or no compliant symbols).

    Used by: compliant_tickers (raised), sync.import_halal (raised), api.upload_halal (-> HTTP 400).
    """

    pass


def to_alpaca_ticker(symbol: str) -> str:
    """Convert a halal-DB ticker to Alpaca/NASDAQ format.

    Args:
        symbol: ticker as written in the halal DB, e.g. "brk-b ".

    Returns:
        Upper-case, trimmed, share class with a dot, e.g. "BRK.B".

    Used by: compliant_tickers.
    """
    # The halal DB writes share classes with a dash (BRK-B); Alpaca and NASDAQ use a dot (BRK.B).
    return symbol.strip().upper().replace("-", ".")


def compliant_tickers(path: Path) -> set[str]:
    """Read the tickers marked COMPLIANT from a halal screening database.

    Args:
        path: SQLite file with a `compliance` table that has `symbol` and `overall_status` columns.
            Opened read-only.

    Returns:
        Compliant tickers in Alpaca format (see to_alpaca_ticker). Every other status
        (NON_COMPLIANT, pending, error, ...) is treated as not halal.

    Raises:
        HalalDbError: the file can't be opened as SQLite or lacks the expected table/columns.

    Used by: sync.sync_symbols (current halal.db), sync.import_halal (a new upload, before it is applied).
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise HalalDbError(f"cannot open halal DB: {e}") from e
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(compliance)")}
        if not {"symbol", "overall_status"} <= cols:
            raise HalalDbError("halal DB must have a 'compliance' table with 'symbol' and 'overall_status' columns")
        rows = conn.execute("SELECT symbol FROM compliance WHERE overall_status = 'COMPLIANT'").fetchall()
    except sqlite3.DatabaseError as e:
        raise HalalDbError(f"not a valid SQLite database: {e}") from e
    finally:
        conn.close()
    return {to_alpaca_ticker(r[0]) for r in rows if r[0]}
