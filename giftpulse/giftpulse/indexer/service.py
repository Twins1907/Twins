"""The polling loop.

One cycle: ask every venue for floors and recent sales on every tracked collection,
write snapshots, run the alert rules over the deltas, dispatch what fires. Repeat on
an interval.

This is the only component that talks to venues on a schedule. The bot and API read
what it wrote, which keeps user-facing latency off the marketplace APIs and means a
venue outage degrades freshness rather than breaking the product.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from giftpulse.alerts.dispatcher import AlertDispatcher
from giftpulse.alerts.engine import AlertEngine, record_sales
from giftpulse.alerts.rules import Alert
from giftpulse.config import Settings, get_settings
from giftpulse.db import init_db, session_scope
from giftpulse.models import FloorSnapshot
from giftpulse.routing.engine import RoutingEngine
from giftpulse.venues.mock import DEMO_COLLECTIONS
from giftpulse.venues.registry import build_adapters, build_client

log = logging.getLogger(__name__)

# Collections the indexer tracks. Kept explicit rather than "everything" because
# poll cost is linear in this list and the long tail has no liquidity worth alerting on.
TRACKED_COLLECTIONS = list(DEMO_COLLECTIONS)


@dataclass
class CycleReport:
    """What one poll produced. Returned so callers can assert on it and so
    ``--once`` can print something useful instead of exiting silently."""

    snapshots: int = 0
    sales_recorded: int = 0
    alerts_found: int = 0
    alerts_sent: int = 0
    venues_responding: int = 0

    def summary(self) -> str:
        return (
            f"{self.snapshots} snapshots, {self.sales_recorded} new sales, "
            f"{self.alerts_found} alerts found, {self.alerts_sent} sent, "
            f"{self.venues_responding} venues responding"
        )


class IndexerService:
    def __init__(
        self,
        settings: Settings | None = None,
        dispatcher: AlertDispatcher | None = None,
        collections: list[str] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.dispatcher = dispatcher or AlertDispatcher(settings=self.settings)
        self.collections = collections or TRACKED_COLLECTIONS
        self.engine = AlertEngine(self.settings)
        self.tick = 0

    async def run_cycle(self) -> CycleReport:
        report = CycleReport()
        client = build_client(self.settings)
        try:
            adapters = build_adapters(client, self.settings, tick=self.tick)
            router = RoutingEngine(adapters, self.settings)
            responding: set[str] = set()

            for collection in self.collections:
                try:
                    await self._process_collection(collection, router, report, responding)
                except Exception:
                    # One bad collection must not abort the cycle — the remaining
                    # collections still have users watching them.
                    log.exception("cycle failed for collection %s", collection)

            report.venues_responding = len(responding)
        finally:
            await client.aclose()

        self.tick += 1
        return report

    async def _process_collection(
        self,
        collection: str,
        router: RoutingEngine,
        report: CycleReport,
        responding: set[str],
    ) -> None:
        floors = await router.floors(collection)
        if not floors:
            return

        alerts: list[Alert] = []

        async with session_scope() as session:
            for quote in floors:
                responding.add(quote.venue)
                alerts.extend(await self.engine.evaluate_floor(session, quote))
                alerts.extend(await self.engine.evaluate_watches(session, quote))
                session.add(
                    FloorSnapshot(
                        venue=quote.venue,
                        collection=collection,
                        floor_ton=quote.floor_ton,
                        listing_count=quote.listing_count,
                        listing_id=quote.listing_id,
                    )
                )
                report.snapshots += 1

            # Spread is computed from the floors already in hand rather than
            # re-polling; the venues were just asked.
            if len(floors) >= 2:
                spread = _spread_from(collection, floors)
                alerts.extend(self.engine.evaluate_spread(spread))

            best_floor = floors[0].floor_ton
            for adapter in router.adapters:
                sales = await adapter.fetch_recent_sales(collection, limit=25)
                if not sales:
                    continue
                report.sales_recorded += await record_sales(session, sales)
                alerts.extend(self.engine.evaluate_sales(sales, best_floor))

            report.alerts_found += len(alerts)
            sent = await self.dispatcher.dispatch(session, alerts)
            report.alerts_sent += len(sent)

    async def run_forever(self) -> None:
        await init_db()
        log.info(
            "indexer started: %d collections, %d venues, %ds interval, mock=%s",
            len(self.collections),
            len(self.settings.enabled_venues),
            self.settings.poll_interval_seconds,
            self.settings.mock,
        )
        while True:
            try:
                report = await self.run_cycle()
                log.info("cycle complete: %s", report.summary())
            except asyncio.CancelledError:
                log.info("indexer stopping")
                raise
            except Exception:
                log.exception("cycle failed entirely, retrying next interval")
            await asyncio.sleep(self.settings.poll_interval_seconds)


def _spread_from(collection: str, floors: list):  # noqa: ANN001, ANN201
    from giftpulse.routing.engine import SpreadQuote

    cheapest, dearest = floors[0], floors[-1]
    if cheapest.venue == dearest.venue:
        return None
    return SpreadQuote(
        collection=collection,
        cheap_venue=cheapest.venue,
        cheap_ton=cheapest.floor_ton,
        rich_venue=dearest.venue,
        rich_ton=dearest.floor_ton,
    )
