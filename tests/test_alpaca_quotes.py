"""Live quotes from Alpaca's real-time SIP snapshots (AlpacaQuotes) and how GET /v1/quotes labels them."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from conftest import FakeAlpaca
from stocksvest_market.alpaca import AlpacaError, AlpacaQuotes
from stocksvest_market.api import create_app
from test_api import READ, seeded  # noqa: F401  (fixture)

SNAPSHOTS = {
    "S0001": {"latestQuote": {"bp": 28.0, "ap": 28.02, "t": "2026-10-26T15:00:01.123456789Z"},
              "latestTrade": {"p": 28.01, "t": "2026-10-26T15:00:00.5Z"}},
    "S0002": {"latestQuote": {"bp": 10.4, "ap": 0, "t": "2026-10-26T15:00:02Z"},  # one-sided: last trade only
              "latestTrade": {"p": 26.0, "t": "2026-10-26T14:59:00Z"}},
}


class Client:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, []

    def _get(self, url, params):
        self.calls.append((url, params))
        if self.error:
            raise self.error
        return self.data


def test_snapshots_become_quotes(settings):
    client = Client(SNAPSHOTS)
    q = AlpacaQuotes(settings, client).quotes(["S0001", "S0002"])
    assert q["S0001"] == {"bid": 28.0, "ask": 28.02, "last": 28.01, "time": "2026-10-26T15:00:01.123456+00:00"}
    assert q["S0002"] == {"bid": None, "ask": None, "last": 26.0, "time": "2026-10-26T14:59:00+00:00"}
    assert client.calls[0][0].endswith("/v2/stocks/snapshots") and client.calls[0][1]["symbols"] == "S0001,S0002"


def test_failure_means_no_quotes_and_shows_in_status(settings):
    source = AlpacaQuotes(settings, Client(error=AlpacaError("500 down")))
    assert source.quotes(["S0001"]) == {} and not source.connected and "500 down" in source.last_error


def test_api_labels_alpaca_quotes(settings, seeded):  # noqa: F811
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000001Z")
    source = AlpacaQuotes(settings, Client({"S0001": {"latestQuote": {"bp": 5.0, "ap": 5.02, "t": now},
                                                      "latestTrade": {"p": 5.01, "t": now}}}))
    with TestClient(create_app(settings, client_factory=lambda s: FakeAlpaca(), quote_source=source)) as c:
        q = c.get("/v1/quotes?symbols=S0001", headers=READ).json()["quotes"]["S0001"]
    assert q["source"] == "ALPACA" and q["price"] == 5.01 and q["ageSeconds"] <= 2
