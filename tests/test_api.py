from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeAlpaca, make_bar, make_halal_db, universe
from stocksvest_market import db, symbols
from stocksvest_market.api import create_app

READ = {"X-API-Key": "reader-key"}
ADMIN = {"X-API-Key": "admin-key"}


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def seeded(conn):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, {"S0001", "S0002"})
    rows = []
    # Daily bars: Fri Oct 9, Mon Oct 12, Tue Oct 13 2026 (midnight New York = 04:00 UTC)
    for i, day in enumerate((9, 12, 13)):
        rows.append(("id-S0001", ts(2026, 10, day, 4), 10 + i, 11 + i, 9 + i, 10 + i, 1000, 5, 10 + i))
    conn.executemany("INSERT INTO bars_1day VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    # Minute bars on Oct 13: one pre-market (08:00 NY), one regular (10:00 NY)
    conn.execute("INSERT INTO bars_1min VALUES ('id-S0001', ?, 12, 12, 12, 12, 5, 1, 12)", (ts(2026, 10, 13, 12),))
    conn.execute("INSERT INTO bars_1min VALUES ('id-S0001', ?, 13, 13, 13, 13, 5, 1, 13)", (ts(2026, 10, 13, 14),))
    conn.execute(
        "INSERT INTO calendar VALUES ('2026-10-13', '2026-10-13T04:00:00-04:00', '2026-10-13T09:30:00-04:00', "
        "'2026-10-13T16:00:00-04:00', '2026-10-13T20:00:00-04:00')"
    )
    conn.commit()
    return conn


@pytest.fixture
def client(settings, seeded):
    with TestClient(create_app(settings, client_factory=lambda s: FakeAlpaca())) as c:
        yield c


def test_auth(client):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/symbols").status_code == 401
    assert client.get("/v1/symbols", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/v1/symbols", headers=READ).status_code == 200
    assert client.get("/v1/symbols", headers=ADMIN).status_code == 200
    assert client.post("/admin/sync", headers=READ).status_code == 401


def test_symbols(client):
    body = client.get("/v1/symbols", headers=READ).json()
    assert body["total"] == 2
    assert body["symbols"][0] == {
        "id": "id-S0001",
        "ticker": "S0001",
        "name": "Stock 1 Inc. Common Stock",
        "exchange": "NASDAQ",
        "assetType": "COMMON_STOCK",
        "sector": None,
        "sic": None,
        "sicDescription": None,
        "halalCompliant": True,
    }
    assert client.get("/v1/symbols/s0001", headers=READ).json()["id"] == "id-S0001"
    assert client.get("/v1/symbols/id-S0002", headers=READ).json()["ticker"] == "S0002"
    assert client.get("/v1/symbols/AAPL", headers=READ).status_code == 404


def test_bars_daily_and_weekly(client):
    daily = client.get("/v1/bars/S0001?timeframe=1Day", headers=READ).json()["bars"]
    assert [b["t"] for b in daily] == ["2026-10-09T04:00:00Z", "2026-10-12T04:00:00Z", "2026-10-13T04:00:00Z"]
    weekly = client.get("/v1/bars/S0001?timeframe=1Week", headers=READ).json()["bars"]
    assert len(weekly) == 2
    assert (weekly[1]["o"], weekly[1]["c"], weekly[1]["v"]) == (11, 12, 2000)
    limited = client.get("/v1/bars/S0001?timeframe=1Day&limit=1", headers=READ).json()["bars"]
    assert limited[0]["t"] == "2026-10-13T04:00:00Z"


def test_bars_session_filter_and_range(client):
    all_bars = client.get("/v1/bars/S0001?timeframe=1Min", headers=READ).json()["bars"]
    assert len(all_bars) == 2
    regular = client.get("/v1/bars/S0001?timeframe=1Min&session=regular", headers=READ).json()["bars"]
    assert [b["c"] for b in regular] == [13]
    ranged = client.get("/v1/bars/S0001?timeframe=1Day&start=2026-10-12&end=2026-10-12", headers=READ).json()["bars"]
    assert [b["c"] for b in ranged] == [11]
    assert client.get("/v1/bars/S0001?start=garbage", headers=READ).status_code == 400


def test_multi_bars(client):
    body = client.get("/v1/bars?symbols=S0001,S0002&timeframe=1Day", headers=READ).json()
    assert len(body["bars"]["S0001"]) == 3 and body["bars"]["S0002"] == []


def test_snapshots(client):
    snap = client.get("/v1/snapshots?symbols=S0001", headers=READ).json()["snapshots"]["S0001"]
    assert snap["latestPrice"] == 13  # latest minute bar beats the daily bar
    assert snap["previousClose"] == 11
    assert snap["dailyChange"] == 2
    assert snap["dailyChangePercent"] == pytest.approx(2 / 11 * 100)
    everyone = client.get("/v1/snapshots", headers=READ).json()["snapshots"]
    assert set(everyone) == {"S0001", "S0002"} and everyone["S0002"]["latestPrice"] is None


def test_price_change(client):
    body = client.get("/v1/price-change?symbols=S0001&periodAmount=3&periodUnit=DAYS", headers=READ).json()
    change = body["changes"]["S0001"]
    assert change["fromTime"] == "2026-10-09T04:00:00Z"
    assert change["toPrice"] == 13 and change["change"] == 3
    minutes = client.get("/v1/price-change?periodAmount=20000&periodUnit=MINUTES", headers=READ)
    assert minutes.status_code == 400


def test_calendar(client):
    days = client.get("/v1/calendar?start=2026-10-13&end=2026-10-13", headers=READ).json()["days"]
    assert days == [
        {
            "date": "2026-10-13",
            "preMarketOpen": "2026-10-13T04:00:00-04:00",
            "regularOpen": "2026-10-13T09:30:00-04:00",
            "regularClose": "2026-10-13T16:00:00-04:00",
            "afterHoursClose": "2026-10-13T20:00:00-04:00",
            "isEarlyClose": False,
        }
    ]


def test_halal_upload_adds_and_removes(settings, seeded, tmp_path, monkeypatch):
    listings, assets = universe()
    monkeypatch.setattr(symbols, "fetch_listings", lambda exchanges: listings)
    day = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)
    fake = FakeAlpaca(assets=assets, bar_source=lambda t, tf, s, e: [make_bar(day, 50.0)] if tf == "1Day" else [])
    app = create_app(settings, client_factory=lambda s: fake)

    # S0001 stays, S0002 becomes non-compliant, S0003 is new (dash format is converted)
    new_db = make_halal_db(tmp_path / "new.db", {"S0001": "COMPLIANT", "S0002": "NON_COMPLIANT", "S0003": "COMPLIANT"})
    with TestClient(app) as c:
        with open(new_db, "rb") as f:
            r = c.post("/admin/halal", headers=ADMIN, files={"file": ("new.db", f)})
        assert r.status_code == 200, r.text
        assert r.json()["added"] == ["S0003"] and r.json()["removed"] == ["S0002"]
        assert r.json()["backfillStarted"] is True

        for _ in range(50):  # backfill runs in a background thread
            if c.get("/v1/bars/S0003", headers=READ).json()["bars"]:
                break
            import time

            time.sleep(0.05)
        assert c.get("/v1/bars/S0003", headers=READ).json()["bars"][0]["c"] == 50.0
        assert c.get("/v1/symbols/S0002", headers=READ).status_code == 404
        assert c.get("/v1/status", headers=READ).json()["lastHalalImport"]["compliant_count"] == 2
    assert settings.halal_db_path.exists()

    with TestClient(app) as c:
        bad = c.post("/admin/halal", headers=ADMIN, files={"file": ("x.db", b"garbage" * 1000)})
        assert bad.status_code == 400
        assert c.post("/admin/halal", headers=READ, files={"file": ("x.db", b"x")}).status_code == 401
