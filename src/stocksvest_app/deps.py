"""Shared request dependencies: settings, market access, DB connection and the access check (API key, or a user
logged in at the trusted login proxy)."""

import sqlite3
from collections.abc import Iterator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Header, HTTPException, Request

from . import db
from .config import Settings
from .brokers.base import Broker
from .brokers.registry import make_broker
from .market.client import MarketAPI
from .market.data import MarketData


def proxy_user(request: Request, trusted_proxies: list[str]) -> str | None:
    """The user the login proxy vouches for: the Remote-User header, but only on a request coming straight from a
    trusted proxy address (anyone else could send that header).

    Args:
        request: the incoming request.
        trusted_proxies: addresses of the login proxy (Settings.trusted_proxies).

    Returns:
        The user name, or None.

    Used by: AppContext.require_key, audit.middleware (the actor of each entry).
    """
    if not request.client or request.client.host not in trusted_proxies:
        return None
    user = (request.headers.get("remote-user") or "").strip()
    return user or None


@dataclass
class AppContext:
    """What every route and background job needs, built once per app.

    Attributes:
        settings: app settings.
        market_api: the market endpoint functions (MarketClient, or a fake in tests).
        market: MarketData helpers on top of market_api (caching, batching); set in __post_init__.
        scheduler: the running AutomationScheduler, or None (tests, APP_RUN_SCHEDULER=false).
        broker_factory: broker account dict -> Broker adapter (registry.make_broker, or a fake in tests).

    Used by: api.create_app (built), every routes.router(ctx), automations.runner/scheduler,
    performance.snapshots, runs.routes.record.
    """

    settings: Settings
    market_api: MarketAPI
    scheduler: object | None = None  # the automation scheduler, when running
    broker_factory: Callable[[dict], Broker] = field(default=make_broker)  # broker account -> adapter

    def __post_init__(self):
        """Wrap market_api in MarketData."""
        self.market = MarketData(self.market_api)

    def conn(self) -> Iterator[sqlite3.Connection]:
        """Dependency: one DB connection per request, closed afterwards."""
        conn = db.connect(self.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_key(self, request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
        """Dependency on every /v1 route: a user logged in at the trusted login proxy, or an APP_API_KEYS value.

        Args:
            request: the incoming request (its address and Remote-User header).
            x_api_key: the X-API-Key header.

        Raises:
            HTTPException: 401 when neither is present and valid.
        """
        if proxy_user(request, self.settings.trusted_proxies):
            return
        if not x_api_key or x_api_key not in self.settings.api_keys:
            raise HTTPException(401, "missing or invalid X-API-Key")
