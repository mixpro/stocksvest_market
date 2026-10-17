"""Create the adapter for a broker account. The only place that knows which class serves which provider."""

import os

from .alpaca import AlpacaBroker
from .base import Broker, BrokerError

PROVIDERS = {"ALPACA"}  # IBKR comes later


def credentials(ref: str) -> tuple[str, str]:
    key, secret = os.environ.get(f"{ref}_KEY", ""), os.environ.get(f"{ref}_SECRET", "")
    if not key or not secret:
        raise BrokerError(f"set {ref}_KEY and {ref}_SECRET in .env")
    return key, secret


def make_broker(account: dict) -> Broker:
    if account["environment"] != "PAPER":
        raise BrokerError("live trading is not enabled; only PAPER accounts can be used")
    if account["provider"] == "ALPACA":
        return AlpacaBroker(*credentials(account["credentialsRef"]), environment=account["environment"])
    raise BrokerError(f"{account['provider']} is not available yet")
