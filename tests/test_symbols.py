import pytest

from conftest import NOW, make_bar, make_halal_db, universe
from stocksvest_market import halal, symbols, sync
from stocksvest_market.symbols import SymbolSyncError, classify, parse_nasdaq_listed, parse_other_listed

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
AACG|ATA Creativity Global - American Depositary Shares, each representing two common shares|S|N|D|100|N|N
AACIW|Armada Acquisition Corp. III - Warrant|G|N|N|100|N|N
AACIU|Armada Acquisition Corp. III - Units|G|N|N|100|N|N
QQQ|Invesco QQQ Trust|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
File Creation Time: 1014202611:01|||||||
"""

OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|40|N|BRK.B
ABR$D|Arbor Realty Trust 6.375% Series D Cumulative Redeemable Preferred Stock|N|ABRpD|N|100|N|ABR-D
AAC.W|Ares Acquisition Corporation III Redeemable warrants|N|AAC.WS|N|100|N|AAC+
AIIA.R|AI Infrastructure Acquisition Corp. Rights|N|AIIAr|N|100|N|AIIA^
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
XYZ|Some Arca Stock Common Stock|P|XYZ|N|100|N|XYZ
File Creation Time: 1014202611:01|||||||
"""


def test_parse_nasdaq_keeps_common_and_adr_only():
    got = {lst.ticker: lst.asset_type for lst in parse_nasdaq_listed(NASDAQ)}
    assert got == {"AAPL": "COMMON_STOCK", "AACG": "ADR"}


def test_parse_other_keeps_nyse_common_only():
    got = parse_other_listed(OTHER, ["N"])
    assert [(lst.ticker, lst.exchange) for lst in got] == [("BRK.B", "NYSE")]


def test_classify_rejects_non_common():
    assert classify("ABC", "ABC Corp 7% Notes due 2030") is None
    assert classify("ABC", "ABC Corp Common Stock") == "COMMON_STOCK"


def test_halal_only_compliant_and_dash_to_dot(tmp_path):
    path = make_halal_db(tmp_path / "h.db", {"BRK-B": "COMPLIANT", "AAPL": "NON_COMPLIANT", "XYZ": "pending"})
    assert halal.compliant_tickers(path) == {"BRK.B"}


def test_halal_rejects_wrong_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    with pytest.raises(halal.HalalDbError):
        halal.compliant_tickers(bad)


def test_reconcile_adds_only_halal_and_listed(conn):
    listings, assets = universe()
    compliant = {"S0001", "S0002", "NOT_LISTED"}
    summary = symbols.reconcile(conn, listings, assets, compliant)
    assert summary["added"] == ["S0001", "S0002"]
    assert {r["ticker"] for r in conn.execute("SELECT ticker FROM symbols")} == {"S0001", "S0002"}


def test_reconcile_skips_untradable(conn):
    listings, assets = universe()
    assets[1]["tradable"] = False  # S0001
    symbols.reconcile(conn, listings, assets, {"S0001", "S0002"})
    assert [r["ticker"] for r in conn.execute("SELECT ticker FROM symbols")] == ["S0002"]


def test_reconcile_removes_non_halal_with_history(conn, settings):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, {"S0001", "S0002", "S0003"})
    client_bars = lambda t, tf, s, e: [make_bar(NOW.replace(hour=14), 10.0)]
    from conftest import FakeAlpaca

    sync.sync_bars(conn, FakeAlpaca(bar_source=client_bars), settings, "1Day", NOW)
    assert conn.execute("SELECT COUNT(*) FROM bars_1day").fetchone()[0] == 3

    summary = symbols.reconcile(conn, listings, assets, {"S0001", "S0002"})
    assert summary["removed"] == ["S0003"]
    assert conn.execute("SELECT COUNT(*) FROM bars_1day WHERE symbol_id = 'id-S0003'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM bars_1day").fetchone()[0] == 2


def test_reconcile_guards_mass_removal(conn):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, {f"S{i:04d}" for i in range(10)})
    with pytest.raises(SymbolSyncError):
        symbols.reconcile(conn, listings, assets, {"S0000"})
    summary = symbols.reconcile(conn, listings, assets, {"S0000"}, force=True)
    assert len(summary["removed"]) == 9


def test_reconcile_refuses_tiny_download(conn):
    listings, assets = universe(10)
    with pytest.raises(SymbolSyncError):
        symbols.reconcile(conn, listings, assets, {"S0001"})


def test_reconcile_follows_ticker_rename(conn):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, {"S0001"})
    assets[1]["symbol"] = "NEW1"
    listings[1] = listings[1].__class__("NEW1", "Renamed Inc.", "NASDAQ", "COMMON_STOCK")
    symbols.reconcile(conn, listings, assets, {"NEW1"})
    row = conn.execute("SELECT id, ticker FROM symbols").fetchone()
    assert (row["id"], row["ticker"]) == ("id-S0001", "NEW1")
