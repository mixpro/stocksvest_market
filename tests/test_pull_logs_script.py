"""scripts/pull_logs.py: paging, limits and time parsing (no server needed)."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("pull_logs", Path(__file__).parent.parent / "scripts" / "pull_logs.py")
pull_logs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pull_logs)


class FakeClient:
    """Serves 25 entries, newest first, honouring limit and beforeId like the API."""

    def __init__(self):
        self.calls = []

    def activity(self, limit, beforeId=None, **filters):
        self.calls.append((limit, beforeId))
        ids = [i for i in range(25, 0, -1) if beforeId is None or i < beforeId][:limit]
        return {"entries": [{"id": i} for i in ids], "nextBeforeId": ids[-1] if len(ids) == limit else None}


def test_fetch_follows_pages(monkeypatch):
    monkeypatch.setattr(pull_logs, "PAGE", 10)
    c = FakeClient()
    assert [e["id"] for e in pull_logs.fetch(c, {}, None)] == list(range(25, 0, -1))
    assert c.calls == [(10, None), (10, 16), (10, 6)]
    c = FakeClient()
    assert [e["id"] for e in pull_logs.fetch(c, {}, 12)] == list(range(25, 13, -1))
    assert c.calls == [(10, None), (2, 16)]


def test_parse_time():
    got = datetime.fromisoformat(pull_logs.parse_time("2h").replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - timedelta(hours=2) - got) < timedelta(seconds=5)
    assert pull_logs.parse_time("2026-10-19T09:30:00-04:00") == "2026-10-19T13:30:00Z"
    assert pull_logs.parse_time(None) is None
    with pytest.raises(SystemExit):
        pull_logs.parse_time("yesterday")
