"""Service settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(name: str, default: str) -> list[str]:
    """Read a comma-separated environment variable as a list, dropping blanks.

    Args:
        name: environment variable name, e.g. "API_KEYS".
        default: value used when the variable is not set.

    Returns:
        The trimmed, non-empty items.

    Used by: Settings (api_keys, other_exchanges).
    """
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    """All market-service settings, read from environment variables when the object is created.

    Attributes:
        alpaca_key / alpaca_secret: Alpaca API credentials (ALPACA_KEY, ALPACA_SECRET). Required for any sync.
        alpaca_data_url: market data API base (ALPACA_DATA_URL).
        alpaca_trading_url: trading API base, used for assets and the calendar (ALPACA_TRADING_URL; paper by default).
        alpaca_feed: bar feed, "sip" (all exchanges) or "iex" (ALPACA_FEED).
        sip_delay_minutes: how far behind "now" bars are requested; 16 on the free plan, 0 on a paid one.
        requests_per_minute: client-side rate limit for Alpaca calls (ALPACA_RPM).
        sec_user_agent: contact sent to SEC EDGAR for sector lookups; empty disables sectors (SEC_USER_AGENT).
        data_dir: folder holding market.db and halal.db (DATA_DIR).
        api_keys: keys accepted on /v1 read endpoints (API_KEYS, comma separated).
        admin_key: key for /admin endpoints, also accepted on /v1 (ADMIN_KEY).
        other_exchanges: exchange codes kept from otherlisted.txt, e.g. ["N"] = NYSE (OTHER_EXCHANGES).
        daily_history_days / hourly_history_days / minute_history_days: how long each bar timeframe is kept.
        run_scheduler: start the background sync loop with the API (RUN_SCHEDULER).
        sync_interval_minutes: time between incremental syncs during the session (SYNC_INTERVAL_MINUTES).
        ibkr_enabled: connect to IB Gateway for live quotes (IBKR_ENABLED; off by default and in tests).
        ibkr_host / ibkr_port: the gateway's API address (IBKR_HOST, IBKR_PORT; 4002 = paper login).
        ibkr_client_id: this service's own API client id, distinct from any other program (IBKR_CLIENT_ID).
        ibkr_live_bars_max: most symbols with real-time IBKR bars at once (IBKR_LIVE_BARS_MAX).

    Used by: create_app (api), main (__main__), AlpacaClient, Scheduler/due_jobs, every sync function, make_client
    (sectors).
    """

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
    # Live quotes from IB Gateway (read-only connection; see ibkr.py).
    ibkr_enabled: bool = field(
        default_factory=lambda: os.environ.get("IBKR_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    ibkr_host: str = field(default_factory=lambda: os.environ.get("IBKR_HOST", "127.0.0.1"))
    ibkr_port: int = field(default_factory=lambda: int(os.environ.get("IBKR_PORT", "4002")))
    ibkr_client_id: int = field(default_factory=lambda: int(os.environ.get("IBKR_CLIENT_ID", "31")))
    # Real-time IBKR bars for at most this many watched symbols (each takes one of ~100 market data lines; live
    # quotes use up to 50 more).
    ibkr_live_bars_max: int = field(default_factory=lambda: int(os.environ.get("IBKR_LIVE_BARS_MAX", "40")))

    @property
    def db_path(self) -> Path:
        """Path of the market database (data_dir/market.db)."""
        return self.data_dir / "market.db"

    @property
    def halal_db_path(self) -> Path:
        """Path of the current halal screening database (data_dir/halal.db)."""
        return self.data_dir / "halal.db"
