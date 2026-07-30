"""Persistence layer.

The indexer writes point-in-time snapshots rather than mutating a "current price"
row. Alert rules are all delta questions ("is this floor 5% below where it was an
hour ago?"), and you cannot ask a delta question of a table that only remembers now.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class FloorSnapshot(Base):
    """Cheapest active listing for a (venue, collection) at a moment in time."""

    __tablename__ = "floor_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    floor_ton: Mapped[float] = mapped_column(Float)
    listing_count: Mapped[int] = mapped_column(Integer, default=0)
    listing_id: Mapped[str] = mapped_column(String(128), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_floor_collection_time", "collection", "captured_at"),
        Index("ix_floor_venue_collection_time", "venue", "collection", "captured_at"),
    )


class SaleEvent(Base):
    """An observed completed sale. Used for whale detection and volume sizing."""

    __tablename__ = "sale_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    gift_id: Mapped[str] = mapped_column(String(128))
    price_ton: Mapped[float] = mapped_column(Float)
    buyer: Mapped[str] = mapped_column(String(96), default="")
    seller: Mapped[str] = mapped_column(String(96), default="")
    # False when the venue reports an internal-balance transfer rather than a
    # settled on-chain trade. Volume metrics must filter on this.
    onchain: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("venue", "gift_id", "occurred_at", name="uq_sale_identity"),
        Index("ix_sale_collection_time", "collection", "occurred_at"),
    )


class AlertLog(Base):
    """Every dispatched alert, for dedupe/cooldown and alert->trade attribution."""

    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    body: Mapped[str] = mapped_column(String(2048))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    """A bot user. Wallet address is public data; keys never touch this process."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    wallet_address: Mapped[str] = mapped_column(String(96), default="")
    referred_by: Mapped[str] = mapped_column(String(64), default="")
    vip_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def is_vip(self, now: datetime | None = None) -> bool:
        if self.vip_until is None:
            return False
        return self.vip_until > (now or utcnow())


class Watch(Base):
    """A user's auto-snipe / floor watch on a collection."""

    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    target_ton: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("telegram_id", "collection", name="uq_watch_user_collection"),
    )


class RoutedTrade(Base):
    """A buy the routing engine built. Recorded when the user is handed a signable
    transaction, confirmed when the chain settles. Never contains key material."""

    __tablename__ = "routed_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    venue: Mapped[str] = mapped_column(String(32))
    collection: Mapped[str] = mapped_column(String(128))
    listing_id: Mapped[str] = mapped_column(String(128))
    price_ton: Mapped[float] = mapped_column(Float)
    fee_ton: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="built", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
