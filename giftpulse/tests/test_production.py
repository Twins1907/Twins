"""Regressions for the failure modes that only show up in production.

Each test here corresponds to a bug that was live in the codebase or a guard that
protects something expensive: a channel that spams itself empty, a metric that
counts window shoppers as revenue, a transaction request every wallet rejects.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from giftpulse.alerts.dispatcher import AlertDispatcher, rank_alerts
from giftpulse.alerts.engine import AlertEngine, record_sales
from giftpulse.alerts.rules import Alert
from giftpulse.analytics import change_pct, floor_history, routed_volume_ton
from giftpulse.maintenance import prune_old_data
from giftpulse.models import AlertLog, FloorSnapshot, RoutedTrade, SaleEvent
from giftpulse.routing.engine import BuyQuote, RoutingEngine
from giftpulse.venues.base import Listing, Sale, VenueAdapter


class RecordingDispatcher(AlertDispatcher):
    def __init__(self, settings=None):  # noqa: ANN001
        super().__init__(bot=None, settings=settings)
        self.delivered: list[Alert] = []

    async def _deliver(self, alert: Alert) -> bool:
        self.delivered.append(alert)
        return True


def _quote(price: float = 100.0, fee: float = 1.0) -> BuyQuote:
    listing = Listing(
        venue="tonnel", collection="Toy Bear", listing_id="L1", price_ton=price
    )
    return BuyQuote(
        listing=listing,
        venue="tonnel",
        price_ton=price,
        fee_ton=fee,
        total_ton=price + fee,
        deep_link="https://example.invalid/l1",
    )


class TestPaymentRequest:
    """``validUntil`` is an absolute UNIX timestamp, not a duration.

    A relative value here reads as January 1970 and every wallet rejects the
    transaction as expired — silently, at the last step of the funnel.
    """

    def test_valid_until_is_an_absolute_future_timestamp(self, settings):
        settings.execution_fee_bps = 100
        settings.fee_wallet = "EQC-fee-wallet"
        engine = RoutingEngine([], settings)

        before = time.time()
        request = engine.build_payment_request(_quote(), wallet_address="EQC-user")

        assert request["validUntil"] > before, "validUntil must be in the future"
        assert request["validUntil"] <= before + 3600, "and not absurdly far ahead"

    def test_fee_leg_amount_is_nanoton(self, settings):
        settings.execution_fee_bps = 100
        settings.fee_wallet = "EQC-fee-wallet"
        engine = RoutingEngine([], settings)

        request = engine.build_payment_request(_quote(fee=1.5), wallet_address="EQC-user")
        assert request["messages"][0]["amount"] == str(int(1.5 * 1_000_000_000))

    def test_a_request_with_no_legs_is_refused(self, settings):
        """A wallet rejects an empty message list, so we must not build one."""
        settings.execution_fee_bps = 0
        engine = RoutingEngine([], settings)

        with pytest.raises(ValueError, match="no on-chain legs"):
            engine.build_payment_request(_quote(fee=0.0), wallet_address="EQC-user")

    def test_a_missing_wallet_is_refused(self, settings):
        engine = RoutingEngine([], settings)
        with pytest.raises(ValueError, match="wallet_address"):
            engine.build_payment_request(_quote(), wallet_address="")


class TestSaleDeduplication:
    """Venues keep a sale in their 'recent' feed for hours.

    Alerting on everything fetched re-fires the same whale buy on every cooldown
    expiry until it finally rolls off the feed.
    """

    def _sale(self, gift_id: str = "g1", price: float = 900.0) -> Sale:
        return Sale(
            venue="tonnel",
            collection="Plush Pepe",
            gift_id=gift_id,
            price_ton=price,
            occurred_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )

    async def test_only_new_sales_are_returned(self, session):
        first = await record_sales(session, [self._sale()])
        await session.commit()
        second = await record_sales(session, [self._sale()])

        assert len(first) == 1
        assert second == [], "a sale already stored must not be reported as new"

    async def test_duplicates_inside_one_batch_are_collapsed(self, session):
        stored = await record_sales(session, [self._sale(), self._sale()])
        assert len(stored) == 1

        await session.commit()
        count = await session.execute(select(func.count(SaleEvent.id)))
        assert count.scalar_one() == 1

    async def test_a_whale_sale_alerts_once_not_every_cycle(self, session, settings):
        engine = AlertEngine(settings)
        whale = self._sale(price=5000.0)

        first_batch = await record_sales(session, [whale])
        await session.commit()
        second_batch = await record_sales(session, [whale])

        assert engine.evaluate_sales(first_batch, floor_ton=100.0), "should alert once"
        assert engine.evaluate_sales(second_batch, floor_ton=100.0) == []


class TestAlertBudget:
    """Cooldown stops the same event repeating. The budget stops thirty distinct
    events from a single market-wide move arriving back to back."""

    def _alerts(self, count: int, kind: str = "floor_break") -> list[Alert]:
        return [
            Alert(
                kind=kind,
                collection=f"C{index}",
                title=f"t{index}",
                body=f"b{index}",
                dedupe_key=f"{kind}:{index}",
            )
            for index in range(count)
        ]

    async def test_the_hourly_cap_holds_the_overflow(self, session, settings):
        settings.alert_max_per_hour = 5
        settings.alert_channel = "@test"
        dispatcher = RecordingDispatcher(settings)

        sent = await dispatcher.dispatch(session, self._alerts(20))

        assert len(sent) == 5
        assert dispatcher.budget_suppressed == 15

    async def test_the_cap_persists_across_dispatches(self, session, settings):
        settings.alert_max_per_hour = 3
        settings.alert_channel = "@test"
        dispatcher = RecordingDispatcher(settings)

        first = await dispatcher.dispatch(session, self._alerts(2))
        second = await dispatcher.dispatch(session, self._alerts(5, kind="spread"))

        assert len(first) == 2
        assert len(second) == 1, "only one slot left in the hour"

    async def test_personal_watch_hits_are_exempt(self, session, settings):
        settings.alert_max_per_hour = 1
        settings.alert_channel = "@test"
        dispatcher = RecordingDispatcher(settings)

        watches = [
            Alert(
                kind="watch_hit",
                collection="Toy Bear",
                title="hit",
                body="hit",
                dedupe_key=f"watch:{index}",
                metadata={"telegram_id": 1000 + index},
            )
            for index in range(6)
        ]
        sent = await dispatcher.dispatch(session, watches)

        # A user asked for this exact price. A busy market must not swallow it.
        assert len(sent) == 6

    async def test_a_zero_cap_disables_the_budget(self, session, settings):
        settings.alert_max_per_hour = 0
        settings.alert_channel = "@test"
        dispatcher = RecordingDispatcher(settings)

        assert len(await dispatcher.dispatch(session, self._alerts(30))) == 30

    def test_actionable_alerts_outrank_context(self):
        alerts = [
            Alert(kind="supply_burst", collection="A", title="", body="", dedupe_key="1"),
            Alert(kind="whale", collection="B", title="", body="", dedupe_key="2"),
            Alert(kind="spread", collection="C", title="", body="", dedupe_key="3"),
            Alert(kind="floor_break", collection="D", title="", body="", dedupe_key="4"),
        ]
        assert [alert.kind for alert in rank_alerts(alerts)] == [
            "spread",
            "floor_break",
            "whale",
            "supply_burst",
        ]


class TestBaselineGuard:
    """A delta needs a baseline old enough to mean something."""

    async def test_a_fresh_snapshot_is_not_a_baseline(self, session, settings):
        from giftpulse.venues.base import FloorQuote

        settings.alert_min_baseline_minutes = 20
        engine = AlertEngine(settings)

        # One snapshot from a minute ago — a brand-new deployment.
        session.add(
            FloorSnapshot(
                venue="tonnel",
                collection="Toy Bear",
                floor_ton=100.0,
                listing_count=10,
                captured_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()

        crashed = FloorQuote(
            venue="tonnel", collection="Toy Bear", floor_ton=50.0, listing_count=10
        )
        alerts = await engine.evaluate_floor(session, crashed)

        assert alerts == [], "a 60-second baseline turns every rule into noise"

    async def test_an_aged_snapshot_is_a_baseline(self, session, settings):
        from giftpulse.venues.base import FloorQuote

        settings.alert_min_baseline_minutes = 20
        settings.floor_break_pct = 5.0
        engine = AlertEngine(settings)

        session.add(
            FloorSnapshot(
                venue="tonnel",
                collection="Toy Bear",
                floor_ton=100.0,
                listing_count=10,
                captured_at=datetime.now(UTC) - timedelta(minutes=45),
            )
        )
        await session.commit()

        crashed = FloorQuote(
            venue="tonnel", collection="Toy Bear", floor_ton=50.0, listing_count=10
        )
        alerts = await engine.evaluate_floor(session, crashed)

        assert [alert.kind for alert in alerts] == ["floor_break"]


class TestRoutedVolume:
    """Routed volume is the number the whole thesis is judged on."""

    async def test_quotes_are_not_counted_as_volume(self, session):
        session.add_all(
            [
                RoutedTrade(
                    telegram_id=1,
                    venue="tonnel",
                    collection="Toy Bear",
                    listing_id="a",
                    price_ton=1000.0,
                    status=RoutedTrade.QUOTED,
                ),
                RoutedTrade(
                    telegram_id=2,
                    venue="tonnel",
                    collection="Toy Bear",
                    listing_id="b",
                    price_ton=250.0,
                    fee_ton=2.5,
                    status=RoutedTrade.CONFIRMED,
                ),
            ]
        )
        await session.commit()

        stats = await routed_volume_ton(session, days=30)

        # Opening the scanner must not move the metric.
        assert stats["routed_volume_ton"] == 250.0
        assert stats["trade_count"] == 1
        assert stats["quote_count"] == 1
        assert stats["quote_to_trade_pct"] == 100.0


class TestChangePct:
    """An unbounded ``min()`` compares the all-time low to itself and reports zero
    change forever."""

    async def test_change_is_measured_between_two_points_in_time(self, session):
        now = datetime.now(UTC)
        session.add_all(
            [
                FloorSnapshot(
                    venue="tonnel",
                    collection="Toy Bear",
                    floor_ton=100.0,
                    listing_count=5,
                    captured_at=now - timedelta(hours=24),
                ),
                FloorSnapshot(
                    venue="tonnel",
                    collection="Toy Bear",
                    floor_ton=80.0,
                    listing_count=5,
                    captured_at=now,
                ),
            ]
        )
        await session.commit()

        assert await change_pct(session, "Toy Bear", hours=24) == -20.0

    async def test_no_baseline_reports_nothing_rather_than_zero(self, session):
        session.add(
            FloorSnapshot(
                venue="tonnel", collection="Toy Bear", floor_ton=80.0, listing_count=5
            )
        )
        await session.commit()

        assert await change_pct(session, "Toy Bear", hours=24) is None


class TestHistoryDownsampling:
    async def test_a_long_window_is_bucketed(self, session):
        now = datetime.now(UTC)
        # Two hours of 60-second polling on one venue: 120 raw rows.
        session.add_all(
            [
                FloorSnapshot(
                    venue="tonnel",
                    collection="Toy Bear",
                    floor_ton=100.0 + index,
                    listing_count=5,
                    captured_at=now - timedelta(minutes=120 - index),
                )
                for index in range(120)
            ]
        )
        await session.commit()

        points = await floor_history(session, "Toy Bear", hours=2, max_points=12)

        # 120 raw rows in, at most 12 out. The bound has to hold despite buckets
        # aligning to absolute epoch rather than to the window start.
        assert 0 < len(points) <= 12, f"expected downsampling, got {len(points)} points"
        assert all("floor_ton" in point for point in points)

    async def test_the_bound_holds_at_every_window_offset(self, session):
        """Epoch-aligned buckets mean the count depends on when 'now' falls."""
        now = datetime.now(UTC)
        session.add_all(
            [
                FloorSnapshot(
                    venue="tonnel",
                    collection="Offset Test",
                    floor_ton=100.0,
                    listing_count=5,
                    captured_at=now - timedelta(seconds=offset),
                )
                for offset in range(0, 7200, 30)
            ]
        )
        await session.commit()

        for max_points in (2, 5, 12, 60, 180):
            points = await floor_history(
                session, "Offset Test", hours=2, max_points=max_points
            )
            assert len(points) <= max_points, (
                f"max_points={max_points} returned {len(points)}"
            )


class TestRetention:
    async def test_old_rows_are_pruned_and_recent_ones_kept(self, session):
        now = datetime.now(UTC)
        session.add_all(
            [
                FloorSnapshot(
                    venue="tonnel",
                    collection="Toy Bear",
                    floor_ton=1.0,
                    listing_count=1,
                    captured_at=now - timedelta(days=90),
                ),
                FloorSnapshot(
                    venue="tonnel",
                    collection="Toy Bear",
                    floor_ton=2.0,
                    listing_count=1,
                    captured_at=now,
                ),
                AlertLog(
                    kind="whale",
                    collection="Toy Bear",
                    dedupe_key="old",
                    body="x",
                    sent_at=now - timedelta(days=90),
                ),
            ]
        )
        await session.commit()

        removed = await prune_old_data(session, retention_days=45)
        await session.commit()

        assert removed["floor_snapshots"] == 1
        assert removed["alert_log"] == 1
        remaining = await session.execute(select(func.count(FloorSnapshot.id)))
        assert remaining.scalar_one() == 1

    async def test_zero_retention_is_a_no_op(self, session):
        assert await prune_old_data(session, retention_days=0) == {}


class _ProbeAdapter(VenueAdapter):
    """Minimal adapter for exercising the transport layer."""

    slug = "probe"

    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        return []

    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        return []


class TestTransport:
    def _adapter(self, handler, **kwargs) -> _ProbeAdapter:  # noqa: ANN001
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return _ProbeAdapter(client, max_rps=0.0, **kwargs)

    async def test_a_rate_limit_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={})
            return httpx.Response(200, json={"ok": True})

        adapter = self._adapter(handler, max_retries=2)
        result = await adapter._request_json("GET", "https://venue.invalid/x")

        assert result == {"ok": True}
        assert calls["n"] == 2, "a 429 must be retried, not treated as an answer"

    async def test_giving_up_returns_none_rather_than_raising(self):
        adapter = self._adapter(lambda _r: httpx.Response(503), max_retries=1)
        assert await adapter._request_json("GET", "https://venue.invalid/x") is None

    async def test_a_rejected_credential_is_flagged_and_not_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={})

        adapter = self._adapter(handler, max_retries=3)
        result = await adapter._request_json("GET", "https://venue.invalid/x")

        assert result is None
        assert adapter.auth_expired is True, "an operator has to be told this died"
        assert calls["n"] == 1, "retrying a bad credential just burns rate budget"

    async def test_a_404_is_an_answer_not_a_retry(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404, json={})

        adapter = self._adapter(handler, max_retries=3)
        assert await adapter._request_json("GET", "https://venue.invalid/x") is None
        assert calls["n"] == 1

    async def test_html_from_a_json_endpoint_does_not_raise(self):
        adapter = self._adapter(lambda _r: httpx.Response(200, text="<html>nope</html>"))
        assert await adapter._request_json("GET", "https://venue.invalid/x") is None


class TestRateLimiter:
    def test_a_burst_is_refused_and_the_window_recovers(self):
        from giftpulse.api.app import RateLimiter

        limiter = RateLimiter(limit=3, window_seconds=60)

        assert [limiter.check(1, now=0.0) for _ in range(4)] == [True, True, True, False]
        # A different user has their own budget.
        assert limiter.check(2, now=0.0) is True
        # And the window slides.
        assert limiter.check(1, now=61.0) is True

    def test_a_zero_limit_disables_the_check(self):
        from giftpulse.api.app import RateLimiter

        limiter = RateLimiter(limit=0)
        assert all(limiter.check(1) for _ in range(100))


class TestHealthStatus:
    def test_status_transitions(self):
        from giftpulse.health import status_for

        assert status_for(None, stall_seconds=600) == "starting"
        assert (
            status_for(
                {
                    "last_success_at": "x",
                    "seconds_since_success": 30.0,
                    "consecutive_failures": 0,
                },
                stall_seconds=600,
            )
            == "ok"
        )
        assert (
            status_for(
                {
                    "last_success_at": "x",
                    "seconds_since_success": 9000.0,
                    "consecutive_failures": 0,
                },
                stall_seconds=600,
            )
            == "degraded"
        ), "a stale heartbeat is the failure that looks like a quiet market"
        assert (
            status_for(
                {
                    "last_success_at": "x",
                    "seconds_since_success": 5.0,
                    "consecutive_failures": 3,
                },
                stall_seconds=600,
            )
            == "degraded"
        )


class TestStartupWarnings:
    def test_a_missing_referral_code_is_called_out(self, settings):
        settings.mock = False
        settings.portals_referral = ""
        settings.tonnel_referral = ""
        settings.mrkt_referral = ""

        warnings = " ".join(settings.startup_warnings())
        assert "referral" in warnings, "the day-one revenue line failing silently is the risk"

    def test_plain_http_public_url_is_called_out(self, settings):
        settings.public_url = "http://giftpulse.example"
        assert any("HTTP" in warning for warning in settings.startup_warnings())
