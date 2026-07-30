"""Routing engine — best execution, fees, and referral attachment."""

from __future__ import annotations

import pytest

from giftpulse.config import Settings
from giftpulse.routing.engine import RoutingEngine
from giftpulse.routing.referral import parse_start_payload, user_referral_code
from giftpulse.venues.base import Listing, Sale, VenueAdapter


class StubVenue(VenueAdapter):
    """A venue with a fixed price, so routing decisions are exactly predictable."""

    def __init__(self, slug: str, price: float, referral: str = "", fails: bool = False):
        super().__init__(client=None, referral_code=referral)
        self.slug = slug
        self.price = price
        self.fails = fails

    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        if self.fails:
            raise RuntimeError(f"{self.slug} is down")
        return [
            Listing(
                venue=self.slug,
                collection=collection,
                listing_id=f"{self.slug}-1",
                price_ton=self.price,
                url=f"https://t.me/{self.slug}/app?startapp=1",
            )
        ]

    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        return []


def settings_with(**overrides) -> Settings:
    base = {
        "mock": True,
        "execution_fee_bps": 0,
        "database_url": "sqlite+aiosqlite:///:memory:",
    }
    return Settings(**{**base, **overrides})


class TestBestBuy:
    async def test_picks_the_cheapest_venue(self):
        engine = RoutingEngine(
            [StubVenue("portals", 100.0), StubVenue("tonnel", 92.0), StubVenue("mrkt", 105.0)],
            settings_with(),
        )
        quote = await engine.best_buy("Toy Bear")
        assert quote.venue == "tonnel"
        assert quote.price_ton == 92.0

    async def test_reports_the_runner_up_and_the_saving(self):
        engine = RoutingEngine(
            [StubVenue("portals", 100.0), StubVenue("tonnel", 92.0)], settings_with()
        )
        quote = await engine.best_buy("Toy Bear")
        assert quote.runner_up_venue == "portals"
        assert quote.savings_ton == 8.0
        assert quote.savings_pct == 8.0

    async def test_respects_the_user_price_limit(self):
        engine = RoutingEngine(
            [StubVenue("portals", 100.0), StubVenue("tonnel", 92.0)], settings_with()
        )
        assert await engine.best_buy("Toy Bear", max_price_ton=80.0) is None

        capped = await engine.best_buy("Toy Bear", max_price_ton=95.0)
        assert capped.venue == "tonnel"

    async def test_a_broken_venue_does_not_break_the_quote(self):
        # The whole point of the adapter layer: one venue failing costs us that
        # venue's price, not the user's ability to trade.
        engine = RoutingEngine(
            [StubVenue("portals", 100.0, fails=True), StubVenue("tonnel", 92.0)],
            settings_with(),
        )
        quote = await engine.best_buy("Toy Bear")
        assert quote.venue == "tonnel"

    async def test_returns_none_when_every_venue_is_down(self):
        engine = RoutingEngine([StubVenue("portals", 100.0, fails=True)], settings_with())
        assert await engine.best_buy("Toy Bear") is None

    async def test_attaches_the_referral_code(self):
        engine = RoutingEngine([StubVenue("tonnel", 92.0, referral="gp123")], settings_with())
        quote = await engine.best_buy("Toy Bear")
        assert "ref=gp123" in quote.deep_link

    async def test_link_is_untouched_without_a_referral_code(self):
        engine = RoutingEngine([StubVenue("tonnel", 92.0)], settings_with())
        quote = await engine.best_buy("Toy Bear")
        assert "ref=" not in quote.deep_link


class TestFees:
    async def test_no_fee_by_default(self):
        engine = RoutingEngine([StubVenue("tonnel", 100.0)], settings_with())
        quote = await engine.best_buy("Toy Bear")
        assert quote.fee_ton == 0.0
        assert quote.total_ton == 100.0

    async def test_one_percent_fee_is_applied(self):
        engine = RoutingEngine([StubVenue("tonnel", 100.0)], settings_with(execution_fee_bps=100))
        quote = await engine.best_buy("Toy Bear")
        assert quote.fee_ton == 1.0
        assert quote.total_ton == 101.0

    async def test_fee_can_erase_the_routing_advantage(self):
        # Routing to a venue 0.5 TON cheaper and charging 2 TON to do it leaves the
        # user worse off. The quote has to say so rather than claim a win.
        engine = RoutingEngine(
            [StubVenue("tonnel", 99.5), StubVenue("portals", 100.0)],
            settings_with(execution_fee_bps=200),
        )
        quote = await engine.best_buy("Toy Bear")
        assert quote.savings_ton == 0.5
        assert quote.total_ton > quote.runner_up_price_ton
        assert quote.beats_runner_up is False

    def test_fee_is_capped_at_a_sane_maximum(self):
        with pytest.raises(ValueError):
            settings_with(execution_fee_bps=1000)


class TestPaymentRequest:
    async def test_builds_a_signable_request_without_touching_keys(self):
        engine = RoutingEngine(
            [StubVenue("tonnel", 100.0)],
            settings_with(execution_fee_bps=100, fee_wallet="EQfee_wallet_address"),
        )
        quote = await engine.best_buy("Toy Bear")
        request = engine.build_payment_request(quote, wallet_address="EQuser_wallet")

        assert request["messages"][0]["address"] == "EQfee_wallet_address"
        # 1 TON expressed in nanoton.
        assert request["messages"][0]["amount"] == "1000000000"

        serialized = str(request).lower()
        for forbidden in ("private", "secret", "mnemonic", "seed"):
            assert forbidden not in serialized

    async def test_refuses_to_build_without_a_wallet(self):
        engine = RoutingEngine([StubVenue("tonnel", 100.0)], settings_with())
        quote = await engine.best_buy("Toy Bear")
        with pytest.raises(ValueError):
            engine.build_payment_request(quote, wallet_address="")


class TestFloors:
    async def test_returns_every_venue_cheapest_first(self):
        engine = RoutingEngine(
            [StubVenue("portals", 100.0), StubVenue("tonnel", 92.0), StubVenue("mrkt", 96.0)],
            settings_with(),
        )
        floors = await engine.floors("Toy Bear")
        assert [floor.venue for floor in floors] == ["tonnel", "mrkt", "portals"]

    async def test_spread_uses_the_extremes(self):
        engine = RoutingEngine(
            [StubVenue("portals", 100.0), StubVenue("tonnel", 92.0), StubVenue("mrkt", 96.0)],
            settings_with(),
        )
        spread = await engine.spread("Toy Bear")
        assert spread.cheap_venue == "tonnel"
        assert spread.rich_venue == "portals"
        assert spread.spread_pct == pytest.approx(8.7, abs=0.05)

    async def test_no_spread_from_a_single_venue(self):
        engine = RoutingEngine([StubVenue("tonnel", 92.0)], settings_with())
        assert await engine.spread("Toy Bear") is None


class TestReferral:
    def test_code_is_stable_and_does_not_leak_the_user_id(self):
        code = user_referral_code(123456789)
        assert code == user_referral_code(123456789)
        assert "123456789" not in code
        assert len(code) == 8

    def test_distinct_users_get_distinct_codes(self):
        assert user_referral_code(1) != user_referral_code(2)

    def test_parses_referral_and_collection_payloads(self):
        assert parse_start_payload("ref_abc123") == {"referrer": "abc123"}
        assert parse_start_payload("c_Plush_Pepe") == {"collection": "Plush Pepe"}
        assert parse_start_payload("ref_abc__c_Toy_Bear") == {
            "referrer": "abc",
            "collection": "Toy Bear",
        }

    def test_empty_payload_is_harmless(self):
        assert parse_start_payload("") == {}
