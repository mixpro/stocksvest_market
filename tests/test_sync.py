from datetime import datetime, timedelta, timezone

from conftest import NOW, FakeAlpaca, make_bar, universe
from stocksvest_market import aggregate, scheduler, symbols, sync


def seed(conn, tickers=("S0001", "S0002")):
    listings, assets = universe()
    symbols.reconcile(conn, listings, assets, set(tickers))


def daily_source(price_fn=lambda t, d: 10.0):
    def source(ticker, timeframe, start, end):
        d = start.replace(hour=4, minute=0, second=0, microsecond=0)
        if d < start:
            d += timedelta(days=1)
        while d <= end:
            if d.weekday() < 5:
                yield make_bar(d, price_fn(ticker, d))
            d += timedelta(days=1)

    return source


def test_backfill_then_incremental(conn, settings):
    seed(conn)
    client = FakeAlpaca(bar_source=daily_source())
    sync.sync_bars(conn, client, settings, "1Day", NOW)
    first_start = client.bar_calls[0][2]
    assert first_start == NOW - timedelta(days=730)
    count = conn.execute("SELECT COUNT(*) FROM bars_1day").fetchone()[0]
    assert count > 2 * 500

    client.bar_calls.clear()
    sync.sync_bars(conn, client, settings, "1Day", NOW + timedelta(days=1))
    (_, _, start, end), = client.bar_calls
    last = conn.execute("SELECT MAX(ts) FROM bars_1day").fetchone()[0]
    assert start <= datetime.fromtimestamp(last, timezone.utc)  # re-fetches the last (maybe partial) bar
    assert end == NOW + timedelta(days=1) - timedelta(minutes=16)  # free-plan SIP delay


def test_new_symbol_gets_full_backfill_in_own_batch(conn, settings):
    seed(conn, ("S0001",))
    client = FakeAlpaca(bar_source=daily_source())
    sync.sync_bars(conn, client, settings, "1Day", NOW)
    seed(conn, ("S0001", "S0002"))
    client.bar_calls.clear()
    sync.sync_bars(conn, client, settings, "1Day", NOW)
    starts = sorted(call[2] for call in client.bar_calls)
    assert starts[0] == NOW - timedelta(days=730)


def test_minute_window_is_one_week(conn, settings):
    seed(conn)
    client = FakeAlpaca()
    sync.sync_bars(conn, client, settings, "1Min", NOW)
    assert client.bar_calls[0][2] == NOW - timedelta(days=7)


def test_find_readjusted_detects_split(conn, settings):
    seed(conn)
    sync.sync_bars(conn, FakeAlpaca(bar_source=daily_source()), settings, "1Day", NOW)
    # S0001 had a 2:1 split: Alpaca now returns halved adjusted prices for all history.
    split = FakeAlpaca(bar_source=daily_source(lambda t, d: 5.0 if t == "S0001" else 10.0))
    assert sync.find_readjusted(conn, split, NOW) == {"id-S0001"}

    sync.sync_all_timeframes(conn, split, settings, NOW, only_ids={"id-S0001"}, full=True)
    closes = {r[0] for r in conn.execute("SELECT DISTINCT c FROM bars_1day WHERE symbol_id = 'id-S0001'")}
    assert closes == {5.0}


def test_prune(conn, settings):
    seed(conn, ("S0001",))
    old = NOW - timedelta(days=8)
    conn.execute("INSERT INTO bars_1min VALUES ('id-S0001', ?, 1, 1, 1, 1, 1, 1, 1)", (int(old.timestamp()),))
    conn.execute("INSERT INTO bars_1min VALUES ('id-S0001', ?, 1, 1, 1, 1, 1, 1, 1)", (int(NOW.timestamp()),))
    sync.prune(conn, settings, NOW)
    assert conn.execute("SELECT COUNT(*) FROM bars_1min").fetchone()[0] == 1


def test_aggregate_week_month_year():
    def bar(day, o, h, l, c, v):
        ts = int(datetime(2026, 10, day, 4, tzinfo=timezone.utc).timestamp())
        return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v, "n": 1, "vw": c}

    # Fri 9th, then Mon 12th - Wed 14th
    daily = [bar(9, 1, 2, 0.5, 1.5, 10), bar(12, 2, 5, 1, 3, 10), bar(13, 3, 4, 2, 2.5, 20), bar(14, 2.5, 3, 0.1, 2, 10)]
    weeks = aggregate.aggregate(daily, "1Week")
    assert len(weeks) == 2
    wk = weeks[1]
    assert (wk["o"], wk["h"], wk["l"], wk["c"], wk["v"], wk["n"]) == (2, 5, 0.1, 2, 40, 3)
    assert wk["vw"] == (3 * 10 + 2.5 * 20 + 2 * 10) / 40
    assert len(aggregate.aggregate(daily, "1Month")) == 1
    assert len(aggregate.aggregate(daily, "1Year")) == 1


def _calendar(conn, day="2026-10-14"):
    conn.execute(
        "INSERT INTO calendar VALUES (?, ?, ?, ?, ?)",
        (day, f"{day}T04:00:00-04:00", f"{day}T09:30:00-04:00", f"{day}T16:00:00-04:00", f"{day}T20:00:00-04:00"),
    )
    conn.commit()


def test_scheduler_first_start_runs_nightly(conn, settings):
    assert scheduler.due_jobs(conn, settings, NOW) == ["nightly"]


def test_scheduler_incremental_during_session(conn, settings):
    seed(conn)
    _calendar(conn)
    assert scheduler.due_jobs(conn, settings, NOW) == ["incremental"]
    sync.db.set_state(conn, "last_incremental_at", (NOW - timedelta(minutes=2)).isoformat())
    assert scheduler.due_jobs(conn, settings, NOW) == []  # interval not reached


def test_scheduler_quiet_before_premarket_and_nightly_after(conn, settings):
    seed(conn)
    _calendar(conn)
    early = datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)  # 03:00 New York
    assert scheduler.due_jobs(conn, settings, early) == []
    late = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)  # 21:00 New York
    assert scheduler.due_jobs(conn, settings, late) == ["nightly"]
    sync.db.set_state(conn, "last_nightly_date", "2026-10-14")
    assert scheduler.due_jobs(conn, settings, late) == []


def test_scheduler_weekend_runs_nightly_only(conn, settings):
    seed(conn)
    _calendar(conn)
    saturday_night = datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)  # Sat 21:00 New York
    assert scheduler.due_jobs(conn, settings, saturday_night) == ["nightly"]


def test_incremental_updates_fresh_symbols_before_backlog(conn, settings):
    seed(conn, ("S0001",))
    client = FakeAlpaca(bar_source=daily_source())
    for tf in ("1Day", "1Hour", "1Min"):
        sync.sync_bars(conn, client, settings, tf, NOW - timedelta(days=1))
    seed(conn, ("S0001", "S0002"))  # S0002 is new: needs a 2-year backfill

    client.bar_calls.clear()
    sync.run_incremental(conn, client, settings, NOW, budget_seconds=0)  # no time left for the backlog
    fetched = {sym for call in client.bar_calls for sym in call[0]}
    assert fetched == {"S0001"}
    assert conn.execute("SELECT COUNT(*) FROM bars_1day WHERE symbol_id = 'id-S0002'").fetchone()[0] == 0

    sync.run_incremental(conn, client, settings, NOW)  # no budget: backlog is finished
    assert conn.execute("SELECT COUNT(*) FROM bars_1day WHERE symbol_id = 'id-S0002'").fetchone()[0] > 500


def test_backlog_resumes_after_deadline(conn, settings):
    seed(conn, ("S0001",))
    client = FakeAlpaca(bar_source=daily_source())
    sync.sync_bars(conn, client, settings, "1Day", NOW, part="backlog", deadline=0)  # deadline already passed
    assert conn.execute("SELECT COUNT(*) FROM bars_1day").fetchone()[0] == 0

    # A partial backfill (oldest bars only) counts as backlog and continues from its last stored bar.
    early = NOW - timedelta(days=700)
    sync.sync_bars(conn, client, settings, "1Day", early)
    last = conn.execute("SELECT MAX(ts) FROM bars_1day").fetchone()[0]
    client.bar_calls.clear()
    sync.sync_bars(conn, client, settings, "1Day", NOW, part="backlog")
    assert client.bar_calls[0][2] == datetime.fromtimestamp(last, timezone.utc)
    assert sync.sync_bars(conn, client, settings, "1Day", NOW, part="backlog") == 0  # now fresh, nothing left
