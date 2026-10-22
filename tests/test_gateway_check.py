"""The IB Gateway support check: parse IBKR's warning, email at 21, 7 and 2 days, each once."""

from datetime import date

import pytest

from stocksvest_market import gateway_check as gc

REAL = ("The version of the application you are running, 1045.1, needs to be upgraded, as it will be desupported on "
        "20261215. The minimum supported version at that time will be 1050.1.")


def test_parse_ibkrs_real_message():
    assert gc.parse_warning(REAL) == {"version": "1045.1", "date": date(2026, 12, 15), "minimum": "1050.1"}
    assert gc.parse_warning("Market data farm connection is OK:usfarm") is None


@pytest.mark.parametrize("days, milestone", [(60, None), (22, None), (21, 21), (8, 21), (7, 7), (3, 7), (2, 2), (0, 2), (-5, 2)])
def test_due_milestone(days, milestone):
    assert gc.due_milestone(days) == milestone


def test_each_reminder_is_sent_once():
    w = gc.parse_warning(REAL)
    state = {}
    sent = []
    for day in (date(2026, 11, 1), date(2026, 11, 24), date(2026, 11, 25), date(2026, 12, 1), date(2026, 12, 8),
                date(2026, 12, 9), date(2026, 12, 13), date(2026, 12, 14), date(2026, 12, 16)):
        key, state = gc.decide(w, day, state)
        if key:
            sent.append((day.isoformat(), key))
    assert sent == [("2026-11-24", "2026-12-15:21"), ("2026-12-08", "2026-12-15:7"), ("2026-12-13", "2026-12-15:2")]


def test_no_warning_clears_the_state():
    assert gc.decide(None, date(2026, 12, 1), {"sent": "2026-12-15:21"}) == (None, {})


def test_a_new_desupport_date_starts_over():
    old = {"sent": "2026-12-15:2"}
    new = gc.parse_warning(REAL.replace("1045.1", "1050.1").replace("20261215", "20270601"))
    assert gc.decide(new, date(2027, 5, 20), old)[0] == "2027-06-01:21"


def test_main_emails_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gc, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(gc, "ibkr_versions", lambda: {"stable": "10.50.1f (x)", "latest": "10.51.1d (y)"})
    sent = []
    monkeypatch.setattr(gc, "send", lambda subject, text: sent.append((subject, text)))

    async def warned(host, port, wait=3.0):
        return gc.parse_warning(REAL.replace("20261215", "20990101")), [REAL]
    monkeypatch.setattr(gc, "read_warning", warned)
    assert gc.main(["--installed", "1045"]) == 0 and sent == []  # far away: nothing yet

    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc).date() + timedelta(days=20)).strftime("%Y%m%d")

    async def warned_soon(host, port, wait=3.0):
        return gc.parse_warning(REAL.replace("20261215", soon)), [REAL]
    monkeypatch.setattr(gc, "read_warning", warned_soon)
    assert gc.main(["--installed", "1045", "--dry-run"]) == 0 and sent == []  # dry run: report only
    assert gc.main(["--installed", "1045"]) == 0 and gc.main(["--installed", "1045"]) == 0
    assert len(sent) == 1 and "in 20 days" in sent[0][0] and "10.50.1f" in sent[0][1] and "1050.1" in sent[0][1]
    assert "emailing" in capsys.readouterr().out


def test_main_reports_an_unreachable_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "STATE", tmp_path / "state.json")

    async def down(host, port, wait=3.0):
        raise ConnectionRefusedError("gateway down")
    monkeypatch.setattr(gc, "read_warning", down)
    assert gc.main([]) == 1 and not (tmp_path / "state.json").exists()
