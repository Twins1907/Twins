"""The polling loop.

One cycle: ask every venue for floors and recent sales on every tracked collection,
write snapshots, run the alert rules over the deltas, dispatch what fires. Repeat on
an interval.

This is the only component that talks to venues on a schedule. The bot and API read
what it wrote, which keeps user-facing latency off the marketplace APIs and means a
venue outage degrades freshness rather than breaking the product.

Two ordering rules matter here and are easy to lose in a refactor:

  * Network calls happen *outside* the database transaction. Holding a transaction
    open across a marketplace round trip is how a single slow venue turns into lock
    contention for everything else.
  * Alerts are ranked and dispatched once per cycle, not once per collection, so the
    hourly channel budget is spent on the best signals across the whole market
    rather than on whichever collection happens to sort first.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field

from giftpulse.alerts.dispatcher import AlertDispatcher
from giftpulse.alerts.engine import AlertEngine, record_sales
from giftpulse.alerts.rules import Alert
from giftpulse.config import Settings, get_settings
from giftpulse.db import init_db, session_scope
from giftpulse.health import INDEXER, record_failure, record_success
from giftpulse.maintenance import prune_old_data
from giftpulse.models import FloorSnapshot
from giftpulse.routing.engine import RoutingEngine, SpreadQuote
from giftpulse.venues.base import FloorQuote, Sale
from giftpulse.venues.mock import DEMO_COLLECTIONS
from giftpulse.venues.registry import VenuePool

log = logging.getLogger(__name__)

# Collections the indexer tracks. Kept explicit rather than "everything" because
# poll cost is linear in this list and the long tail has no liquidity worth alerting on.
TRACKED_COLLECTIONS = list(DEMO_COLLECTIONS)

# Housekeeping runs on its own clock, not every cycle. At the default 60-second
# interval this is roughly hourly.
_PRUNE_EVERY_CYCLES = 60

# Consecutive failures before the admin chat hears about it. One failed cycle is a
# venue having a moment; three in a row is an outage.
_FAILURES_BEFORE_ALARM = 3


@dataclass
class CycleReport:
    """What one poll produced. Returned so callers can assert on it and so
    ``--once`` can print something useful instead of exiting silently."""

    snapshots: int = 0
    sales_recorded: int = 0
    alerts_found: int = 0
    alerts_sent: int = 0
    venues_responding: int = 0
    expired_credentials: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"{self.snapshots} snapshots, {self.sales_recorded} new sales, "
            f"{self.alerts_found} alerts found, {self.alerts_sent} sent, "
            f"{self.venues_responding} venues responding"
        )
        if self.expired_credentials:
            text += f", credentials rejected: {', '.join(self.expired_credentials)}"
        return text


class IndexerService:
    def __init__(
        self,
        settings: Settings | None = None,
        dispatcher: AlertDispatcher | None = None,
        collections: list[str] | None = None,
        pool: VenuePool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.dispatcher = dispatcher or AlertDispatcher(settings=self.settings)
        self.collections = collections or TRACKED_COLLECTIONS
        self.engine = AlertEngine(self.settings)
        self.tick = 0
        self._pool = pool
        self._owns_pool = pool is None
        # Credentials already reported, so a dead authData produces one message
        # rather than one per cycle forever.
        self._reported_credentials: set[str] = set()

    @property
    def pool(self) -> VenuePool:
        if self._pool is None:
            self._pool = VenuePool(self.settings)
        return self._pool

    async def aclose(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.aclose()
            self._pool = None

    # --- one cycle --------------------------------------------------------

    async def run_cycle(self) -> CycleReport:
        report = CycleReport()
        adapters = self.pool.adapters_for_tick(self.tick)
        router = RoutingEngine(adapters, self.settings)
        responding: set[str] = set()
        alerts: list[Alert] = []

        for collection in self.collections:
            try:
                alerts.extend(
                    await self._process_collection(collection, router, report, responding)
                )
            except Exception:
                # One bad collection must not abort the cycle — the remaining
                # collections still have users watching them.
                log.exception("cycle failed for collection %s", collection)

        report.venues_responding = len(responding)
        report.alerts_found = len(alerts)
        report.expired_credentials = self.pool.expired_credentials()

        if alerts:
            # A session of its own, opened after the snapshot writes have committed:
            # delivery takes 3.5 seconds per message and must not sit inside a
            # transaction that is holding row locks.
            async with session_scope() as session:
                sent = await self.dispatcher.dispatch(session, alerts)
            report.alerts_sent = len(sent)

        # Recorded per cycle rather than per loop iteration, so `indexer --once`
        # from cron marks liveness exactly the same way the daemon does.
        async with session_scope() as session:
            await record_success(session, INDEXER, report.summary())

        self.tick += 1
        return report

    async def _process_collection(
        self,
        collection: str,
        router: RoutingEngine,
        report: CycleReport,
        responding: set[str],
    ) -> list[Alert]:
        # --- network, outside any transaction ---
        floors = await router.floors(collection)
        if not floors:
            return []

        sales_per_venue = await asyncio.gather(
            *(adapter.fetch_recent_sales(collection, limit=25) for adapter in router.adapters),
            return_exceptions=True,
        )
        fetched_sales: list[Sale] = []
        for result in sales_per_venue:
            if isinstance(result, BaseException):
                log.warning("%s: sales lookup failed: %s", collection, result)
                continue
            fetched_sales.extend(result)

        # --- database, no network in scope ---
        alerts: list[Alert] = []
        best_floor = floors[0].floor_ton

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
                alerts.extend(self.engine.evaluate_spread(_spread_from(collection, floors)))

            # Only sales we had not already stored reach the whale rule. Venues keep
            # a sale in their "recent" feed for hours, so evaluating everything
            # fetched re-fires the same whale buy on every cooldown expiry.
            new_sales = await record_sales(session, fetched_sales)
            report.sales_recorded += len(new_sales)
            alerts.extend(self.engine.evaluate_sales(new_sales, best_floor))

        return alerts

    # --- the loop ---------------------------------------------------------

    async def run_forever(self) -> None:
        await init_db()
        for warning in self.settings.startup_warnings():
            log.warning("startup: %s", warning)

        log.info(
            "indexer started: %d collections, %d venues, %ds interval, mock=%s",
            len(self.collections),
            len(self.settings.enabled_venues),
            self.settings.poll_interval_seconds,
            self.settings.mock,
        )

        cycles = 0
        try:
            while True:
                try:
                    report = await self.run_cycle()
                    log.info("cycle complete: %s", report.summary())
                    await self._report_expired_credentials(report)
                except asyncio.CancelledError:
                    log.info("indexer stopping")
                    raise
                except Exception as exc:
                    log.exception("cycle failed entirely, retrying next interval")
                    await self._record_cycle_failure(exc)

                cycles += 1
                if cycles % _PRUNE_EVERY_CYCLES == 0:
                    await self._prune()

                await asyncio.sleep(self._next_interval())
        finally:
            await self.aclose()

    def _next_interval(self) -> float:
        """Poll interval with jitter.

        A fixed interval means every venue call lands on the same second of every
        minute. Spreading the load is the difference between looking like a
        well-behaved client and looking like something to rate-limit.
        """
        base = self.settings.poll_interval_seconds
        jitter = base * self.settings.poll_jitter_pct
        return max(1.0, base + random.uniform(-jitter, jitter))

    async def _prune(self) -> None:
        try:
            async with session_scope() as session:
                await prune_old_data(session, self.settings.retention_days)
        except Exception:
            # Housekeeping must never take the indexer down with it.
            log.exception("pruning failed; continuing")

    async def _record_cycle_failure(self, exc: Exception) -> None:
        try:
            async with session_scope() as session:
                failures = await record_failure(session, INDEXER, f"{type(exc).__name__}: {exc}")
        except Exception:
            log.exception("could not record cycle failure")
            return

        if failures == _FAILURES_BEFORE_ALARM:
            await self.dispatcher.notify_admin(
                f"⚠️ GiftPulse indexer has failed {failures} cycles in a row.\n"
                f"Last error: {type(exc).__name__}: {exc}"
            )

    async def _report_expired_credentials(self, report: CycleReport) -> None:
        """Tell the operator once per credential that a venue has gone dark.

        Silent degradation from three venues to two is a revenue loss wearing the
        costume of normal operation, which is exactly the failure that never gets
        noticed on its own.
        """
        fresh = [
            slug
            for slug in report.expired_credentials
            if slug not in self._reported_credentials
        ]
        if not fresh:
            return
        self._reported_credentials.update(fresh)
        await self.dispatcher.notify_admin(
            "🔑 GiftPulse credentials rejected for: <b>"
            + ", ".join(fresh)
            + "</b>\nThat venue is returning nothing until it is re-authenticated."
        )


def _spread_from(collection: str, floors: list[FloorQuote]) -> SpreadQuote | None:
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
