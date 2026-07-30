"""End-to-end: indexer cycle -> snapshots -> alerts, over the mock venues."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from giftpulse.alerts.dispatcher import AlertDispatcher
from giftpulse.alerts.rules import Alert
from giftpulse.indexer.service import IndexerService
from giftpulse.models import AlertLog, FloorSnapshot, SaleEvent


class RecordingDispatcher(AlertDispatcher):
    """Dispatcher that records instead of sending, so a cycle is fully offline."""

    def __init__(self, settings=None):  # noqa: ANN001
        super().__init__(bot=None, settings=settings)
        self.delivered: list[Alert] = []

    async def _deliver(self, alert: Alert) -> bool:
        self.delivered.append(alert)
        return True


@pytest.fixture
async def prepared_db():
    from giftpulse.config import get_settings
    from giftpulse.db import dispose_db, init_db

    get_settings.cache_clear()
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


class TestIndexerCycle:
    async def test_a_cycle_writes_snapshots_for_every_venue(self, prepared_db, session):
        from giftpulse.config import get_settings

        settings = get_settings()
        service = IndexerService(
            settings=settings,
            dispatcher=RecordingDispatcher(settings),
            collections=["Plush Pepe", "Toy Bear"],
        )
        report = await service.run_cycle()

        assert report.snapshots == len(settings.enabled_venues) * 2
        assert report.venues_responding == len(settings.enabled_venues)

        count = await session.execute(select(func.count(FloorSnapshot.id)))
        assert count.scalar_one() == report.snapshots

    async def test_a_cycle_records_sales(self, prepared_db, session):
        service = IndexerService(
            dispatcher=RecordingDispatcher(), collections=["Plush Pepe"]
        )
        report = await service.run_cycle()

        assert report.sales_recorded > 0
        stored = await session.execute(select(func.count(SaleEvent.id)))
        assert stored.scalar_one() == report.sales_recorded

    async def test_sales_are_not_double_counted_across_cycles(self, prepared_db):
        # The mock venue returns the same sales for a given tick, so re-running the
        # same tick must store nothing new.
        service = IndexerService(
            dispatcher=RecordingDispatcher(), collections=["Plush Pepe"]
        )
        first = await service.run_cycle()
        service.tick = 0  # replay the same tick
        second = await service.run_cycle()

        assert first.sales_recorded > 0
        assert second.sales_recorded == 0

    async def test_repeated_cycles_produce_alerts(self, prepared_db):
        """Floors move across ticks, so deltas eventually cross a threshold."""
        dispatcher = RecordingDispatcher()
        service = IndexerService(
            dispatcher=dispatcher, collections=["Plush Pepe", "Toy Bear", "Astral Shard"]
        )
        for _ in range(6):
            await service.run_cycle()

        assert dispatcher.delivered, "expected the mock market to generate at least one alert"
        kinds = {alert.kind for alert in dispatcher.delivered}
        assert kinds <= {"floor_break", "spread", "whale", "supply_burst", "watch_hit"}


class TestCooldown:
    async def test_the_same_alert_is_not_sent_twice(self, prepared_db, session):
        dispatcher = RecordingDispatcher()
        alert = Alert(
            kind="floor_break",
            collection="Toy Bear",
            title="t",
            body="b",
            dedupe_key="floor_break:Toy Bear:tonnel:40",
        )

        first = await dispatcher.dispatch(session, [alert])
        await session.commit()
        second = await dispatcher.dispatch(session, [alert])

        assert len(first) == 1
        assert second == []

        logged = await session.execute(select(func.count(AlertLog.id)))
        assert logged.scalar_one() == 1

    async def test_distinct_events_both_send(self, prepared_db, session):
        dispatcher = RecordingDispatcher()
        alerts = [
            Alert(kind="floor_break", collection="A", title="a", body="a", dedupe_key="k1"),
            Alert(kind="floor_break", collection="B", title="b", body="b", dedupe_key="k2"),
        ]
        sent = await dispatcher.dispatch(session, alerts)
        assert len(sent) == 2


class TestAnalytics:
    async def test_overview_ranks_by_spread(self, prepared_db, session):
        from giftpulse.analytics import collection_overview

        service = IndexerService(
            dispatcher=RecordingDispatcher(),
            collections=["Plush Pepe", "Toy Bear", "Swiss Watch"],
        )
        await service.run_cycle()

        overview = await collection_overview(
            session, ["Plush Pepe", "Toy Bear", "Swiss Watch"]
        )
        assert len(overview) == 3
        spreads = [item["spread_pct"] for item in overview]
        assert spreads == sorted(spreads, reverse=True)

        for item in overview:
            # The headline price must be the cheapest venue, not an arbitrary one.
            assert item["best_floor_ton"] == min(
                venue["floor_ton"] for venue in item["venues"]
            )

    async def test_onchain_volume_excludes_internal_transfers(self, prepared_db, session):
        from datetime import UTC, datetime

        from giftpulse.analytics import onchain_volume_ton

        session.add_all(
            [
                SaleEvent(
                    venue="tonnel",
                    collection="Toy Bear",
                    gift_id="1",
                    price_ton=100.0,
                    onchain=True,
                    occurred_at=datetime.now(UTC),
                ),
                SaleEvent(
                    venue="tonnel",
                    collection="Toy Bear",
                    gift_id="2",
                    price_ton=900.0,
                    onchain=False,
                    occurred_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

        # 900 TON of internal-balance movement must not inflate the number.
        assert await onchain_volume_ton(session, "Toy Bear", days=7) == 100.0
