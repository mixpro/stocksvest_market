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
    settings: Settings
    market_api: MarketAPI
    scheduler: object | None = None  # the automation scheduler, when running
    broker_factory: Callable[[dict], Broker] = field(default=make_broker)  # broker account -> adapter

    def __post_init__(self):
        self.market = MarketData(self.market_api)

    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = db.connect(self.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_key(self, x_api_key: Annotated[str | None, Header()] = None) -> None:
        if not x_api_key or x_api_key not in self.settings.api_keys:
            raise HTTPException(401, "missing or invalid X-API-Key")
