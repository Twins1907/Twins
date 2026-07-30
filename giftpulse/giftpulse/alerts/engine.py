"""Evaluates alert rules against what the indexer has stored."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from giftpulse.alerts.rules import (
    Alert,
    floor_break,
    new_listing_burst,
    spread_alert,
    watch_hit,
    whale_buy,
)
from giftpulse.config import Settings, get_settings
from giftpulse.models import FloorSnapshot, SaleEvent, Watch
from giftpulse.routing.engine import SpreadQuote
from giftpulse.venues.base import FloorQuote, Sale

log = logging.getLogger(__name__)


class AlertEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def evaluate_floor(
        self,
        session: AsyncSession,
        current: FloorQuote,
        lookback_minutes: int = 60,
    ) -> list[Alert]:
        """Compare a fresh floor against where the same venue was an hour ago."""
        previous = await self._previous_snapshot(
            session, current.venue, current.collection, lookback_minutes
        )
        if previous is None:
            return []

        alerts: list[Alert] = []
        break_alert = floor_break(
            current.collection, previous.floor_ton, current, self.settings.floor_break_pct
        )
        if break_alert:
            alerts.append(break_alert)

        burst = new_listing_burst(current.collection, previous.listing_count, current)
        if burst:
            alerts.append(burst)
        return alerts

    def evaluate_spread(self, spread: SpreadQuote | None) -> list[Alert]:
        if spread is None:
            return []
        alert = spread_alert(spread, self.settings.spread_alert_pct)
        return [alert] if alert else []

    def evaluate_sales(self, sales: list[Sale], floor_ton: float) -> list[Alert]:
        alerts: list[Alert] = []
        for sale in sales:
            alert = whale_buy(sale, floor_ton, self.settings.whale_min_ton)
            if alert:
                alerts.append(alert)
        return alerts

    async def evaluate_watches(
        self, session: AsyncSession, current: FloorQuote
    ) -> list[Alert]:
        """Per-user auto-snipe targets met by this floor."""
        rows = await session.execute(
            select(Watch).where(
                Watch.collection == current.collection,
                Watch.active.is_(True),
                Watch.target_ton >= current.floor_ton,
            )
        )
        alerts: list[Alert] = []
        for watch in rows.scalars():
            alert = watch_hit(current.collection, watch.target_ton, current, watch.telegram_id)
            if alert:
                alerts.append(alert)
        return alerts

    async def _previous_snapshot(
        self,
        session: AsyncSession,
        venue: str,
        collection: str,
        lookback_minutes: int,
    ) -> FloorSnapshot | None:
        """The most recent snapshot at least ``lookback_minutes`` old.

        Deliberately not "the previous row": comparing against the last poll makes
        every rule a 60-second momentum detector that fires on ordinary churn.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
        result = await session.execute(
            select(FloorSnapshot)
            .where(
                FloorSnapshot.venue == venue,
                FloorSnapshot.collection == collection,
                FloorSnapshot.captured_at <= cutoff,
            )
            .order_by(FloorSnapshot.captured_at.desc())
            .limit(1)
        )
        snapshot = result.scalar_one_or_none()
        if snapshot is not None:
            return snapshot

        # Cold start: nothing that old exists yet. Fall back to the oldest row we
        # have, but only once it is old enough to be a real baseline. Comparing
        # against a snapshot from the previous poll turns every rule into a
        # 60-second momentum detector and buries a launch-day channel in noise.
        floor_age = datetime.now(UTC) - timedelta(
            minutes=self.settings.alert_min_baseline_minutes
        )
        result = await session.execute(
            select(FloorSnapshot)
            .where(
                FloorSnapshot.venue == venue,
                FloorSnapshot.collection == collection,
                FloorSnapshot.captured_at <= floor_age,
            )
            .order_by(FloorSnapshot.captured_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()


def _as_utc(moment: datetime) -> datetime:
    """Normalize a timestamp for comparison.

    SQLite hands back naive datetimes even from a ``DateTime(timezone=True)``
    column, so identity comparisons have to meet on common ground or every stored
    sale looks new on the next poll.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def record_sales(session: AsyncSession, sales: list[Sale]) -> list[Sale]:
    """Persist sales we have not already seen. Returns the ones newly stored.

    Returning the new sales rather than a count is what stops the whale rule
    re-alerting: venues keep a sale in their "recent" feed for hours, so evaluating
    every fetched sale re-fires the same whale buy on every cooldown expiry until it
    finally rolls off the feed.

    Existence is checked with one query for the whole batch. The obvious
    row-at-a-time version costs a round trip per sale — roughly 600 per cycle at
    eight collections across three venues, which is most of a cycle's time budget
    spent asking SQLite the same question.
    """
    if not sales:
        return []

    oldest = min(_as_utc(sale.occurred_at) for sale in sales)
    existing_rows = await session.execute(
        select(SaleEvent.venue, SaleEvent.gift_id, SaleEvent.occurred_at).where(
            SaleEvent.venue.in_({sale.venue for sale in sales}),
            SaleEvent.gift_id.in_({sale.gift_id for sale in sales}),
            SaleEvent.occurred_at >= oldest,
        )
    )
    seen = {
        (venue, gift_id, _as_utc(occurred_at)) for venue, gift_id, occurred_at in existing_rows
    }

    stored: list[Sale] = []
    for sale in sales:
        identity = (sale.venue, sale.gift_id, _as_utc(sale.occurred_at))
        # Guard against duplicates inside this batch too, not just against the
        # table — a venue occasionally returns the same sale twice in one page.
        if identity in seen:
            continue
        seen.add(identity)
        session.add(
            SaleEvent(
                venue=sale.venue,
                collection=sale.collection,
                gift_id=sale.gift_id,
                price_ton=sale.price_ton,
                buyer=sale.buyer,
                seller=sale.seller,
                onchain=sale.onchain,
                occurred_at=sale.occurred_at,
            )
        )
        stored.append(sale)
    return stored
