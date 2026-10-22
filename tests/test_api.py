from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeAlpaca, make_bar, make_halal_db, universe
from stocksvest_market import db, symbols
from stocksvest_market.api import _market_time_back, _trading_days_back, create_app
from stocksvest_market.sync import ET

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


def add_calendar(conn, *days):
    conn.executemany("INSERT OR IGNORE INTO calendar VALUES (?, ?, ?, ?, ?)", [
        (d, f"{d}T04:00:00-04:00", f"{d}T09:30:00-04:00", f"{d}T16:00:00-04:00", f"{d}T20:00:00-04:00") for d in days])
    conn.commit()


def change_of(client, amount, unit):
    url = f"/v1/price-change?symbols=S0001&periodAmount={amount}&periodUnit={unit}"
    return client.get(url, headers=READ).json()["changes"]["S0001"]


def test_price_change(client, seeded):
    add_calendar(seeded, "2026-10-09", "2026-10-12")  # Fri, (weekend), Mon, Tue
    # Latest price: Tue Oct 13 10:00 New York (13). 1 day = Tuesday's move, from Monday's close.
    one = change_of(client, 1, "DAYS")
    assert (one["fromTime"], one["fromPrice"], one["toPrice"], one["change"]) == ("2026-10-12T04:00:00Z", 11, 13, 2)
    # 2 trading days = Monday + Tuesday, from Friday's close: the weekend doesn't count.
    two = change_of(client, 2, "DAYS")
    assert (two["fromTime"], two["change"]) == ("2026-10-09T04:00:00Z", 3)
    assert change_of(client, 3, "DAYS")["change"] is None  # the calendar doesn't reach that far back
    minutes = client.get("/v1/price-change?periodAmount=20000&periodUnit=MINUTES", headers=READ)
    assert minutes.status_code == 400


def test_price_change_skips_closed_hours(client, seeded):
    add_calendar(seeded, "2026-10-09", "2026-10-12")
    # A Friday 19:30 New York minute bar. The latest price is Tue 10:00.
    seeded.execute("INSERT INTO bars_1min VALUES ('id-S0001', ?, 7, 7, 7, 7, 5, 1, 7)", (ts(2026, 10, 9, 23, 30),))
    seeded.commit()
    assert change_of(client, 120, "MINUTES")["fromTime"] == "2026-10-13T12:00:00Z"  # Tue 08:00 pre-market bar
    # 6h (Tue 04:00-10:00) + 16h (all of Monday's session) + 30 min reaches Friday 19:30, not Sunday.
    out = change_of(client, 22 * 60 + 30, "MINUTES")
    assert (out["fromTime"], out["fromPrice"], out["change"]) == ("2026-10-09T23:30:00Z", 7, 6)


def test_snapshot_previous_close_in_pre_market(client, seeded):
    add_calendar(seeded, "2026-10-09", "2026-10-12", "2026-10-14")
    # Wednesday 08:00 New York, pre-market: no Wednesday daily bar yet, the newest is Tuesday's (close 12).
    seeded.execute("INSERT INTO bars_1min VALUES ('id-S0001', ?, 15, 15, 15, 15, 5, 1, 15)", (ts(2026, 10, 14, 12),))
    seeded.commit()
    snap = client.get("/v1/snapshots?symbols=S0001", headers=READ).json()["snapshots"]["S0001"]
    assert snap["dailyDate"] == "2026-10-13" and snap["latestPrice"] == 15
    assert snap["previousClose"] == 12 and snap["dailyChange"] == 3  # Tuesday's close, not Monday's (11)


def test_snapshot_previous_close_after_a_weekend(client, seeded):
    add_calendar(seeded, "2026-10-09", "2026-10-12")
    seeded.execute("DELETE FROM bars_1min")  # latest price = Tuesday's daily close (12)
    seeded.execute("DELETE FROM bars_1day WHERE ts = ?", (ts(2026, 10, 12, 4),))  # no Monday bar (e.g. halted)
    seeded.commit()
    snap = client.get("/v1/snapshots?symbols=S0001", headers=READ).json()["snapshots"]["S0001"]
    assert snap["previousClose"] == 10  # the last close before Tuesday: Friday's


def test_volume(client, seeded):
    # S0001 traded 1000 shares on Fri 9 (vw 10), Mon 12 (vw 11) and Tue 13 (vw 12); S0002 never.
    add_calendar(seeded, "2026-10-08", "2026-10-09", "2026-10-12", "2099-01-02")  # 2099: not completed yet
    seeded.execute("INSERT INTO bars_1day VALUES ('id-S0001', ?, 50, 50, 50, 50, 99999, 1, 50)", (ts(2099, 1, 2, 5),))
    seeded.commit()
    body = client.get("/v1/volume?days=4", headers=READ).json()
    assert (body["from"], body["to"]) == ("2026-10-08", "2026-10-13")
    s1 = body["volumes"]["S0001"]
    assert s1["averageVolume"] == 750 and s1["daysTraded"] == 3  # Thu 8 had no trades: counts as 0
    assert s1["averageDollarVolume"] == pytest.approx((10000 + 11000 + 12000) / 4)
    assert body["volumes"]["S0002"] == {"averageVolume": 0.0, "averageDollarVolume": 0.0, "daysTraded": 0}
    one = client.get("/v1/volume?days=1&symbols=S0001", headers=READ).json()
    assert one["volumes"]["S0001"]["averageVolume"] == 1000 and set(one["volumes"]) == {"S0001"}
    short = client.get("/v1/volume?days=10&symbols=S0001", headers=READ).json()
    assert short["from"] is None and short["volumes"]["S0001"]["averageVolume"] is None


def test_trading_days_back():
    days = ["2026-11-30", "2026-11-27", "2026-11-25", "2026-11-24"]  # Thanksgiving (26th) and the weekend closed
    assert _trading_days_back(days, "2026-11-30", 1) == "2026-11-27"  # Monday: from Friday's close
    assert _trading_days_back(days, "2026-11-30", 2) == "2026-11-25"  # Fri + Mon, from Wednesday's close
    assert _trading_days_back(days, "2026-11-29", 1) == "2026-11-25"  # on Sunday, Friday is day 1
    assert _trading_days_back(days, "2026-11-30", 4) is None


def test_market_time_back():
    def session(d):
        return (datetime.fromisoformat(f"{d}T04:00").replace(tzinfo=ET), datetime.fromisoformat(f"{d}T20:00").replace(tzinfo=ET))

    sessions = [session("2026-10-12"), session("2026-10-09")]  # Mon, Fri
    monday_6am = datetime(2026, 10, 12, 6, tzinfo=ET)
    assert _market_time_back(sessions, monday_6am, timedelta(hours=1)) == datetime(2026, 10, 12, 5, tzinfo=ET)
    # 3 hours from Monday 06:00: 2 on Monday, the 3rd is Friday 19:00-20:00.
    assert _market_time_back(sessions, monday_6am, timedelta(hours=3)) == datetime(2026, 10, 9, 19, tzinfo=ET)
    sunday = datetime(2026, 10, 11, 12, tzinfo=ET)  # closed: counts from Friday's session end
    assert _market_time_back(sessions, sunday, timedelta(hours=2)) == datetime(2026, 10, 9, 18, tzinfo=ET)
    assert _market_time_back(sessions, monday_6am, timedelta(hours=40)) is None


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
