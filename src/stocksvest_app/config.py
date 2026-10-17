"""App service settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    market_url: str = field(default_factory=lambda: os.environ.get("MARKET_URL", "http://localhost:8000"))
    market_api_key: str = field(default_factory=lambda: os.environ.get("MARKET_API_KEY", ""))
    # Keys clients (and the sample UI) send in X-API-Key, comma separated.
    api_keys: list[str] = field(
        default_factory=lambda: [k.strip() for k in os.environ.get("APP_API_KEYS", "").split(",") if k.strip()]
    )
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "data")))
    # Run history (filters now; automations and other actions later) is kept this long for analysis.
    run_retention_days: int = field(default_factory=lambda: int(os.environ.get("RUN_RETENTION_DAYS", "183")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"
