"""Create the adapter for a broker account. The only place that knows which class serves which provider."""

import os

from .alpaca import AlpacaBroker
from .base import Broker, BrokerError

PROVIDERS = {"ALPACA"}  # IBKR comes later. Used by: brokers.routes (account provider check)


def live_allowed() -> bool:
    """Real-money accounts need ALLOW_LIVE=1 in .env (only on the production host).

    Used by: make_broker, brokers.routes (account environment check).
    """
    return os.environ.get("ALLOW_LIVE") == "1"


def credentials(ref: str) -> tuple[str, str]:
    """The API key and secret of a broker account, from the environment (.env).

    Args:
        ref: the account's credentialsRef; reads <ref>_KEY and <ref>_SECRET.

    Returns:
        (key, secret).

    Raises:
        BrokerError: either is missing.

    Used by: make_broker.
    """
    key, secret = os.environ.get(f"{ref}_KEY", ""), os.environ.get(f"{ref}_SECRET", "")
    if not key or not secret:
        raise BrokerError(f"set {ref}_KEY and {ref}_SECRET in .env")
    return key, secret


def make_broker(account: dict) -> Broker:
    """The adapter for a broker account. LIVE accounts only when live_allowed().

    Args:
        account: the broker account dict (provider, environment, credentialsRef).

    Raises:
        BrokerError: live account, unknown provider or missing credentials.

    Used by: api.create_app (the default deps.AppContext.broker_factory; tests pass a fake).
    """
    if account["environment"] != "PAPER" and not live_allowed():
        raise BrokerError("live trading is not enabled (ALLOW_LIVE=1); only PAPER accounts can be used")
    if account["provider"] == "ALPACA":
        return AlpacaBroker(*credentials(account["credentialsRef"]), environment=account["environment"])
    raise BrokerError(f"{account['provider']} is not available yet")
