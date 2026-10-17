from fastapi.testclient import TestClient

from conftest import FakeAlpaca, universe
from stocksvest_market import sectors, symbols
from stocksvest_market.api import create_app

READ = {"X-API-Key": "reader-key"}


class FakeSec:
    def __init__(self):
        self.lookups = []

    def cik_by_ticker(self):
        return {"S0001": 1, "S0002": 2, "S0003": 3}

    def sic(self, cik):
        self.lookups.append(cik)
        return {1: (3571, "Electronic Computers"), 2: (2834, "Pharmaceutical Preparations"), 3: (None, None)}[cik]


def test_sector_for_sic():
    assert sectors.sector_for_sic(3571) == "TECHNOLOGY"
    assert sectors.sector_for_sic(7372) == "TECHNOLOGY"
    assert sectors.sector_for_sic(2834) == "HEALTHCARE"
    assert sectors.sector_for_sic(1311) == "ENERGY"
    assert sectors.sector_for_sic(3634) == "CONSUMER_DISCRETIONARY"  # appliances, not electronics
    assert sectors.sector_for_sic(3714) == "CONSUMER_DISCRETIONARY"  # auto parts
    assert sectors.sector_for_sic(6798) == "REAL_ESTATE"
    assert sectors.sector_for_sic(4911) == "UTILITIES"
    assert sectors.sector_for_sic(None) == "OTHER"
    assert sectors.sector_for_sic(9999) == "OTHER"


def test_sync_sectors_only_missing(conn):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, {"S0001", "S0002", "S0003", "S0004"})
    sec = FakeSec()
    assert sectors.sync_sectors(conn, sec) == {"updated": 4, "unknown": 2}  # S0003 no SIC, S0004 no CIK
    rows = {r["ticker"]: (r["sic"], r["sector"]) for r in conn.execute("SELECT * FROM symbols")}
    assert rows == {
        "S0001": (3571, "TECHNOLOGY"),
        "S0002": (2834, "HEALTHCARE"),
        "S0003": (None, "OTHER"),
        "S0004": (None, "OTHER"),
    }
    sec.lookups.clear()
    assert sectors.sync_sectors(conn, sec) == {"updated": 0, "unknown": 0}
    assert sec.lookups == []


def test_api_sectors(settings, conn):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, {"S0001", "S0002", "S0003"})
    sectors.sync_sectors(conn, FakeSec())
    with TestClient(create_app(settings, client_factory=lambda s: FakeAlpaca())) as c:
        body = c.get("/v1/sectors", headers=READ).json()
        tech = next(s for s in body["sectors"] if s["key"] == "TECHNOLOGY")
        assert tech == {"key": "TECHNOLOGY", "name": "Technology", "symbols": 1}
        assert body["unassigned"] == 0
        got = c.get("/v1/symbols?sector=TECHNOLOGY,HEALTHCARE", headers=READ).json()
        assert [s["ticker"] for s in got["symbols"]] == ["S0001", "S0002"]
        assert got["symbols"][0]["sicDescription"] == "Electronic Computers"
