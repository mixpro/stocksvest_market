"""Service settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    alpaca_key: str = field(default_factory=lambda: os.environ.get("ALPACA_KEY", ""))
    alpaca_secret: str = field(default_factory=lambda: os.environ.get("ALPACA_SECRET", ""))
    alpaca_data_url: str = field(
        default_factory=lambda: os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    )
    alpaca_trading_url: str = field(
        default_factory=lambda: os.environ.get("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets")
    )
    alpaca_feed: str = field(default_factory=lambda: os.environ.get("ALPACA_FEED", "sip"))
    # Free plan: SIP data from the last 15 minutes is not available. Set 0 on a paid plan.
    sip_delay_minutes: int = field(default_factory=lambda: int(os.environ.get("SIP_DELAY_MINUTES", "16")))
    requests_per_minute: int = field(default_factory=lambda: int(os.environ.get("ALPACA_RPM", "180")))

    # SEC EDGAR requires a contact in the User-Agent, e.g. "StocksVest-Market you@example.com".
    # Empty = sector sync is skipped.
    sec_user_agent: str = field(default_factory=lambda: os.environ.get("SEC_USER_AGENT", ""))

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "data")))

    # Read API keys for third-party apps (comma separated) and the admin key for uploads.
    api_keys: list[str] = field(default_factory=lambda: _list("API_KEYS", ""))
    admin_key: str = field(default_factory=lambda: os.environ.get("ADMIN_KEY", ""))

    # NASDAQ symbol-directory exchange codes kept from otherlisted.txt (N = NYSE).
    other_exchanges: list[str] = field(default_factory=lambda: _list("OTHER_EXCHANGES", "N"))

    daily_history_days: int = field(default_factory=lambda: int(os.environ.get("DAILY_HISTORY_DAYS", "730")))
    hourly_history_days: int = field(default_factory=lambda: int(os.environ.get("HOURLY_HISTORY_DAYS", "730")))
    minute_history_days: int = field(default_factory=lambda: int(os.environ.get("MINUTE_HISTORY_DAYS", "7")))

    run_scheduler: bool = field(
        default_factory=lambda: os.environ.get("RUN_SCHEDULER", "true").lower() in ("1", "true", "yes")
    )
    sync_interval_minutes: int = field(default_factory=lambda: int(os.environ.get("SYNC_INTERVAL_MINUTES", "5")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "market.db"

    @property
    def halal_db_path(self) -> Path:
        return self.data_dir / "halal.db"
