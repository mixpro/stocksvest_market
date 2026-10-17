"""Shared request dependencies: settings, market access, DB connection and API-key check."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException

from . import db
from .config import Settings
from .market.client import MarketAPI
from .market.data import MarketData


@dataclass
class AppContext:
    settings: Settings
    market_api: MarketAPI

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
