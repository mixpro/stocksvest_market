"""Shared request dependencies: settings, market access, DB connection and API-key check."""

import sqlite3
from collections.abc import Iterator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Header, HTTPException

from . import db
from .config import Settings
from .brokers.base import Broker
from .brokers.registry import make_broker
from .market.client import MarketAPI
from .market.data import MarketData


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

    def require_key(self, x_api_key: Annotated[str | None, Header()] = None) -> None:
        """Dependency on every /v1 route: accept only APP_API_KEYS values.

        Args:
            x_api_key: the X-API-Key header.

        Raises:
            HTTPException: 401 when missing or wrong.
        """
        if not x_api_key or x_api_key not in self.settings.api_keys:
            raise HTTPException(401, "missing or invalid X-API-Key")
