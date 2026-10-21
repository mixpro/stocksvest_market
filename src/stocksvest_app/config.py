"""App service settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """All app-service settings, read from environment variables when the object is created.

    Attributes:
        market_url: base URL of the market service (MARKET_URL; http://market:8000 in Docker Compose).
        market_api_key: the key this app sends to the market service (MARKET_API_KEY).
        api_keys: keys accepted from clients in X-API-Key (APP_API_KEYS, comma separated).
        trusted_proxies: addresses whose Remote-User header counts as a logged-in user instead of an API key: the
            login proxy in front of the app (APP_TRUSTED_PROXIES, comma separated; e.g. the VPS's Tailscale IP).
        data_dir: folder holding app.db (DATA_DIR).
        run_retention_days: how long the run history is kept (RUN_RETENTION_DAYS, default 183 = ~6 months).
        run_scheduler: start the automation scheduler with the API (APP_RUN_SCHEDULER; off in tests).
        scheduler_tick_seconds: time between scheduler passes (SCHEDULER_TICK_SECONDS).
        alerts_enabled: email alerts and the daily summary (ALERTS_ENABLED; off by default and in tests).
        alert_email_to: who receives them (ALERT_EMAIL_TO).
        alert_email_from: the sender (ALERT_EMAIL_FROM; default "StocksVest <alerts@stocksvest.net>").
        smtp_host / smtp_port / smtp_username / smtp_password: the mail relay (SMTP_*; Brevo, STARTTLS on 587).

    Used by: api.create_app, deps.AppContext, market.client.MarketClient, __main__.main.
    """

    market_url: str = field(default_factory=lambda: os.environ.get("MARKET_URL", "http://localhost:8000"))
    market_api_key: str = field(default_factory=lambda: os.environ.get("MARKET_API_KEY", ""))
    # Keys clients (and the sample UI) send in X-API-Key, comma separated.
    api_keys: list[str] = field(
        default_factory=lambda: [k.strip() for k in os.environ.get("APP_API_KEYS", "").split(",") if k.strip()]
    )
    # The login proxy (Caddy + Authelia on the VPS) checks the user and passes it in Remote-User; trusted only from here.
    trusted_proxies: list[str] = field(
        default_factory=lambda: [a.strip() for a in os.environ.get("APP_TRUSTED_PROXIES", "").split(",") if a.strip()]
    )
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "data")))
    # Run history (filters now; automations and other actions later) is kept this long for analysis.
    run_retention_days: int = field(default_factory=lambda: int(os.environ.get("RUN_RETENTION_DAYS", "183")))

    # Background scheduler for automations (off in tests).
    run_scheduler: bool = field(
        default_factory=lambda: os.environ.get("APP_RUN_SCHEDULER", "true").lower() in ("1", "true", "yes")
    )
    scheduler_tick_seconds: int = field(default_factory=lambda: int(os.environ.get("SCHEDULER_TICK_SECONDS", "30")))

    # Email alerts (alerts/): problems once when they start and once when resolved, plus a summary after the close.
    alerts_enabled: bool = field(
        default_factory=lambda: os.environ.get("ALERTS_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    alert_email_to: str = field(default_factory=lambda: os.environ.get("ALERT_EMAIL_TO", ""))
    alert_email_from: str = field(
        default_factory=lambda: os.environ.get("ALERT_EMAIL_FROM", "StocksVest <alerts@stocksvest.net>")
    )
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_username: str = field(default_factory=lambda: os.environ.get("SMTP_USERNAME", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""), repr=False)

    @property
    def db_path(self) -> Path:
        """Path of the app database (data_dir/app.db)."""
        return self.data_dir / "app.db"
