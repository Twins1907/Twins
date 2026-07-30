"""Runtime configuration.

Everything is read from the environment with a ``GIFTPULSE_`` prefix so the same
image runs locally, on a VPS, or in CI without code changes. See ``.env.example``.

There is deliberately no setting anywhere in this file that accepts a private key,
seed phrase, or wallet secret. GiftPulse is routing-only; if a future setting looks
like it wants custody, that is the bug.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GIFTPULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    bot_token: str = ""
    alert_channel: str = ""
    # Chat that receives operational notices (indexer stalled, Portals auth expired).
    # Separate from alert_channel: users must never see our plumbing.
    admin_chat_id: str = ""

    # Storage
    database_url: str = "sqlite+aiosqlite:///./giftpulse.db"
    # Run Alembic to head on startup. Leave on for a single-VPS deployment; turn it
    # off when a separate release step owns migrations.
    auto_migrate: bool = True
    # Snapshots/sales/alert history older than this are pruned. The indexer writes
    # ~35k snapshot rows a day, so "keep everything" is not a policy.
    retention_days: int = 45

    # Mini App / API
    public_url: str = "http://localhost:8080"
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Indexer
    poll_interval_seconds: int = 60
    enabled_venues: list[str] = Field(default_factory=lambda: ["portals", "tonnel", "mrkt"])
    mock: bool = True
    request_timeout_seconds: float = 15.0
    # Spread polls over a window instead of firing every venue call on the same
    # second of every minute. A fleet of identical bots hitting :00 is what gets an
    # IP rate-limited.
    poll_jitter_pct: float = 0.15
    # Ceiling on sustained request rate to any one venue, across the whole process.
    venue_max_rps: float = 2.0
    # Retries for a 429/5xx/transport failure. Beyond this the venue is treated as
    # down for this cycle, which is a normal weather condition, not an error.
    venue_max_retries: int = 3
    # A cycle taking longer than this to complete means the indexer is stalled;
    # /api/health reports degraded and the admin chat is notified once.
    cycle_stall_seconds: int = 600

    # Venue credentials — referral codes and short-lived auth blobs only.
    portals_auth_data: str = ""
    portals_referral: str = ""
    tonnel_referral: str = ""
    mrkt_referral: str = ""

    # Monetization
    execution_fee_bps: int = 0
    fee_wallet: str = ""

    # Alert thresholds
    floor_break_pct: float = 5.0
    spread_alert_pct: float = 8.0
    whale_min_ton: float = 500.0
    alert_cooldown_seconds: int = 900
    # Hard ceiling on channel posts per hour. Cooldown stops the *same* event
    # repeating; this stops a market-wide move posting thirty distinct events back
    # to back. The channel is the top of the funnel — one spammy hour costs
    # subscribers permanently. Personal watch-hit DMs are exempt.
    alert_max_per_hour: int = 12
    # A delta is only meaningful against a baseline this old. Without it, a fresh
    # deployment compares against a snapshot from the previous poll and every rule
    # degenerates into a 60-second momentum detector.
    alert_min_baseline_minutes: int = 20

    # API abuse control. /api/quote fans out to every venue, so it is the endpoint
    # that turns one rude user into a venue-side rate-limit problem.
    quote_rate_limit_per_minute: int = 20

    @field_validator("enabled_venues", mode="before")
    @classmethod
    def _split_venues(cls, value: object) -> object:
        if isinstance(value, str):
            return [slug.strip().lower() for slug in value.split(",") if slug.strip()]
        return value

    @field_validator("execution_fee_bps")
    @classmethod
    def _sane_fee(cls, value: int) -> int:
        if not 0 <= value <= 300:
            raise ValueError("execution_fee_bps must be between 0 and 300 (0–3%)")
        return value

    @property
    def fee_rate(self) -> float:
        """Execution fee as a plain multiplier, e.g. 100 bps -> 0.01."""
        return self.execution_fee_bps / 10_000

    @property
    def fee_enabled(self) -> bool:
        return self.execution_fee_bps > 0 and bool(self.fee_wallet)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_memory_db(self) -> bool:
        return ":memory:" in self.database_url

    def startup_warnings(self) -> list[str]:
        """Misconfigurations worth shouting about at boot.

        Returned rather than raised: a half-configured deployment should still come
        up and serve what it can, but the operator needs to see why it is degraded
        instead of discovering it from a silent revenue line.
        """
        problems: list[str] = []

        if self.mock:
            problems.append(
                "GIFTPULSE_MOCK=true — serving synthetic prices. "
                "No real market data is being indexed."
            )
        else:
            if "portals" in self.enabled_venues and not self.portals_auth_data:
                problems.append(
                    "portals is enabled but GIFTPULSE_PORTALS_AUTH_DATA is empty; "
                    "that venue will return nothing."
                )
            if not any(
                (
                    self.portals_referral,
                    self.tonnel_referral,
                    self.mrkt_referral,
                )
            ):
                problems.append(
                    "no venue referral codes set — trades will route without "
                    "attribution and earn nothing. This is the day-one revenue line."
                )

        if self.public_url.startswith("http://") and "localhost" not in self.public_url:
            problems.append(
                f"GIFTPULSE_PUBLIC_URL is plain HTTP ({self.public_url}); "
                "Telegram will refuse to open the Mini App."
            )

        if self.fee_enabled and not self.fee_wallet:
            problems.append("an execution fee is set but GIFTPULSE_FEE_WALLET is empty.")

        if not self.admin_chat_id:
            problems.append(
                "GIFTPULSE_ADMIN_CHAT_ID is unset — operational alerts "
                "(stalled indexer, expired credentials) have nowhere to go."
            )

        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
