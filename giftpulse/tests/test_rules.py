"""Alert rules — the logic that decides what users see."""

from __future__ import annotations

from datetime import UTC, datetime

from giftpulse.alerts.rules import (
    floor_break,
    format_for_telegram,
    new_listing_burst,
    spread_alert,
    watch_hit,
    whale_buy,
)
from giftpulse.routing.engine import SpreadQuote
from giftpulse.venues.base import FloorQuote, Sale


def quote(floor: float, venue: str = "portals", count: int = 10) -> FloorQuote:
    return FloorQuote(
        venue=venue, collection="Plush Pepe", floor_ton=floor, listing_count=count
    )


class TestFloorBreak:
    def test_fires_on_a_drop_past_the_threshold(self):
        alert = floor_break("Plush Pepe", 4000.0, quote(3600.0), threshold_pct=5.0)
        assert alert is not None
        assert alert.kind == "floor_break"
        assert "10.0%" in alert.title

    def test_ignores_a_drop_under_the_threshold(self):
        assert floor_break("Plush Pepe", 4000.0, quote(3900.0), threshold_pct=5.0) is None

    def test_ignores_rises_entirely(self):
        # A floor jumping 40% is not an execution opportunity; alerting on it is noise.
        assert floor_break("Plush Pepe", 4000.0, quote(5600.0), threshold_pct=5.0) is None

    def test_ignores_a_zero_baseline(self):
        assert floor_break("Plush Pepe", 0.0, quote(100.0), threshold_pct=5.0) is None

    def test_dedupe_key_is_stable_for_the_same_event(self):
        first = floor_break("Plush Pepe", 4000.0, quote(3600.0), threshold_pct=5.0)
        second = floor_break("Plush Pepe", 4000.0, quote(3600.04), threshold_pct=5.0)
        assert first.dedupe_key == second.dedupe_key


class TestSpread:
    def test_fires_when_the_gap_is_wide(self):
        spread = SpreadQuote("Toy Bear", "tonnel", 40.0, "mrkt", 46.0)
        alert = spread_alert(spread, threshold_pct=8.0)
        assert alert is not None
        assert alert.venue == "tonnel"
        assert "15.0%" in alert.title

    def test_silent_when_the_gap_is_narrow(self):
        assert spread_alert(SpreadQuote("Toy Bear", "tonnel", 40.0, "mrkt", 41.0), 8.0) is None


class TestWhale:
    def test_fires_on_a_large_sale_well_above_floor(self):
        sale = Sale(venue="portals", collection="Plush Pepe", gift_id="1", price_ton=9000.0)
        alert = whale_buy(sale, floor_ton=4000.0, min_ton=500.0)
        assert alert is not None
        assert "2.2× floor" in alert.body

    def test_ignores_a_sale_below_the_absolute_floor_threshold(self):
        sale = Sale(venue="portals", collection="Toy Bear", gift_id="1", price_ton=120.0)
        assert whale_buy(sale, floor_ton=40.0, min_ton=500.0) is None

    def test_ignores_a_large_sale_that_is_merely_at_floor(self):
        # A 5000 TON sale on a 4900 TON floor is an ordinary trade, not conviction.
        sale = Sale(venue="portals", collection="Plush Pepe", gift_id="1", price_ton=5000.0)
        assert whale_buy(sale, floor_ton=4900.0, min_ton=500.0) is None


class TestWatchHit:
    def test_fires_at_or_below_target(self):
        alert = watch_hit("Toy Bear", 40.0, quote(38.0, venue="tonnel"), telegram_id=7)
        assert alert is not None
        assert alert.metadata["telegram_id"] == 7

    def test_silent_above_target(self):
        assert watch_hit("Toy Bear", 40.0, quote(44.0), telegram_id=7) is None


class TestSupplyBurst:
    def test_fires_when_listings_flood_in(self):
        alert = new_listing_burst("Toy Bear", previous_count=10, current=quote(40.0, count=22))
        assert alert is not None
        assert "+12" in alert.title

    def test_silent_on_ordinary_churn(self):
        assert new_listing_burst("Toy Bear", 10, quote(40.0, count=12)) is None

    def test_silent_without_a_baseline(self):
        assert new_listing_burst("Toy Bear", 0, quote(40.0, count=30)) is None


class TestFormatting:
    def test_escapes_html_from_collection_names(self):
        sale = Sale(
            venue="portals",
            collection="<script>alert(1)</script>",
            gift_id="1",
            price_ton=9000.0,
            occurred_at=datetime.now(UTC),
        )
        alert = whale_buy(sale, floor_ton=1000.0, min_ton=500.0)
        rendered = format_for_telegram(alert)
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered

    def test_includes_links_when_available(self):
        current = FloorQuote(
            venue="tonnel",
            collection="Toy Bear",
            floor_ton=38.0,
            listing_count=5,
            url="https://t.me/tonnel/app?startapp=1",
        )
        alert = floor_break("Toy Bear", 44.0, current, threshold_pct=5.0)
        rendered = format_for_telegram(alert, bot_username="giftpulse_bot")
        assert "Open listing" in rendered
        assert "start=c_Toy_Bear" in rendered
