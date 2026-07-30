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

    # Storage
    database_url: str = "sqlite+aiosqlite:///./giftpulse.db"

    # Mini App / API
    public_url: str = "http://localhost:8080"
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Indexer
    poll_interval_seconds: int = 60
    enabled_venues: list[str] = Field(default_factory=lambda: ["portals", "tonnel", "mrkt"])
    mock: bool = True
    request_timeout_seconds: float = 15.0

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
