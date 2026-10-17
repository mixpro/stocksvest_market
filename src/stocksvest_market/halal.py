"""Read the halal screening database. Only symbols with overall_status = COMPLIANT are kept."""

import sqlite3
from pathlib import Path


class HalalDbError(ValueError):
    pass


def to_alpaca_ticker(symbol: str) -> str:
    # The halal DB writes share classes with a dash (BRK-B); Alpaca and NASDAQ use a dot (BRK.B).
    return symbol.strip().upper().replace("-", ".")


def compliant_tickers(path: Path) -> set[str]:
    """Return compliant tickers in Alpaca format. Raises HalalDbError if the file is not a valid halal DB."""
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
