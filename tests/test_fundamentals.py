"""SEC fundamentals and earnings dates: sync from frames / submissions, then as-of lookups."""

import sqlite3
from datetime import date

import requests

from stocksvest_market import fundamentals


class FakeSec:
    def __init__(self):
        self.urls = []

    def _get(self, url):
        self.urls.append(url)
        if "submissions" in url:
            return {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K"], "filingDate": ["2026-07-30", "2026-07-31",
                                                                                         "2026-05-01"],
                                           "items": ["2.02,9.01", "", "5.02"]}}}
        if "/NetIncomeLoss/USD/CY2025." in url:
            return {"data": [{"cik": 1, "end": "2025-12-31", "val": 20.0}, {"cik": 99, "end": "2025-12-31", "val": 1}]}
        if "/StockholdersEquity/USD/CY2025Q4I" in url:
            return {"data": [{"cik": 1, "end": "2025-12-31", "val": 100.0}]}
        raise requests.HTTPError("404")


def test_sync_and_as_of():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE symbols (id TEXT, ticker TEXT, cik INTEGER)")
    conn.execute("INSERT INTO symbols VALUES ('id-A', 'A', 1)")
    sec = FakeSec()
    out = fundamentals.sync(conn, sec, date(2026, 10, 25))
    assert out == {"facts": 2, "earnings": 1}  # CIK 99 is not one of our symbols
    facts, earnings = fundamentals.load(conn)
    assert earnings == {1: ["2026-07-30"]}  # only the 8-K with item 2.02
    v = fundamentals.as_of(facts.get(1), earnings.get(1), "2026-10-25")
    assert v["roe"] == 20.0 and v["daysSinceEarnings"] == 87 and v["daysToEarnings"] == 4
    assert fundamentals.load(sqlite3.connect(":memory:")) == ({}, {})  # never synced
