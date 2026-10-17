import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stocksvest_market import db
from stocksvest_market.config import Settings
from stocksvest_market.symbols import Listing


class FakeAlpaca:
    """In-memory stand-in for AlpacaClient. `bar_source(ticker, timeframe, start, end)` returns bars."""

    def __init__(self, assets=None, bar_source=None, calendar_days=None):
        self._assets = assets or []
        self.bar_source = bar_source or (lambda *a: [])
        self.calendar_days = calendar_days or []
        self.bar_calls: list[tuple] = []

    def assets(self):
        return self._assets

    def calendar(self, start, end):
        return self.calendar_days

    def bars(self, symbols, timeframe, start, end):
        self.bar_calls.append((tuple(symbols), timeframe, start, end))
        for t in symbols:
            for bar in self.bar_source(t, timeframe, start, end):
                yield t, bar


def make_bar(ts: datetime, close: float, volume: float = 100.0) -> dict:
    return {
        "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": close,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": volume,
        "n": 10,
        "vw": close,
    }


def universe(n: int = 1200) -> tuple[list[Listing], list[dict]]:
    """n NASDAQ listings plus matching Alpaca assets (reconcile refuses tiny inputs)."""
    listings = [Listing(f"S{i:04d}", f"Stock {i} Inc. Common Stock", "NASDAQ", "COMMON_STOCK") for i in range(n)]
    assets = [{"id": f"id-{lst.ticker}", "symbol": lst.ticker, "tradable": True, "status": "active"} for lst in listings]
    return listings, assets


def make_halal_db(path: Path, statuses: dict[str, str]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE compliance (symbol TEXT PRIMARY KEY, name TEXT, overall_status TEXT, "
        "aaoifi_status TEXT, purification REAL, checked_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO compliance(symbol, name, overall_status) VALUES (?, ?, ?)",
        [(s, s, st) for s, st in statuses.items()],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_KEYS", "reader-key")
    monkeypatch.setenv("ADMIN_KEY", "admin-key")
    monkeypatch.setenv("SIP_DELAY_MINUTES", "16")
    monkeypatch.setenv("RUN_SCHEDULER", "false")
    return Settings()


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    db.init(c)
    yield c
    c.close()


NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)  # Wednesday 14:00 New York
