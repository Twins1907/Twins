"""Best-execution routing.

Given a collection, ask every enabled venue what it has, and return the cheapest
executable listing with our referral code attached and the fee accounted for.

The engine builds a *quote*, not a transaction that moves anyone's money. The user
signs in their own wallet via TON Connect; this process never sees a key. That is the
architectural line the whole product is built on — everything else here is detail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from giftpulse.config import Settings, get_settings
from giftpulse.venues.base import FloorQuote, Listing, VenueAdapter

log = logging.getLogger(__name__)

# How long a signable quote stays valid. Short on purpose: floors move.
_QUOTE_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class BuyQuote:
    """Everything the user needs to decide, and the bot needs to build a deep link."""

    listing: Listing
    venue: str
    price_ton: float
    fee_ton: float
    total_ton: float
    deep_link: str
    # Cheapest competing venue, if any — this is what makes the pitch concrete.
    runner_up_venue: str = ""
    runner_up_price_ton: float = 0.0

    @property
    def savings_ton(self) -> float:
        """How much cheaper than the next-best venue, before our fee."""
        if not self.runner_up_price_ton:
            return 0.0
        return round(self.runner_up_price_ton - self.price_ton, 2)

    @property
    def savings_pct(self) -> float:
        if not self.runner_up_price_ton:
            return 0.0
        return round(self.savings_ton / self.runner_up_price_ton * 100, 2)

    @property
    def beats_runner_up(self) -> bool:
        """True when routing here still wins after our fee is added."""
        if not self.runner_up_price_ton:
            return True
        return self.total_ton < self.runner_up_price_ton


@dataclass(frozen=True, slots=True)
class SpreadQuote:
    collection: str
    cheap_venue: str
    cheap_ton: float
    rich_venue: str
    rich_ton: float

    @property
    def spread_ton(self) -> float:
        return round(self.rich_ton - self.cheap_ton, 2)

    @property
    def spread_pct(self) -> float:
        if self.cheap_ton <= 0:
            return 0.0
        return round(self.spread_ton / self.cheap_ton * 100, 2)


class RoutingEngine:
    def __init__(self, adapters: list[VenueAdapter], settings: Settings | None = None) -> None:
        self.adapters = adapters
        self.settings = settings or get_settings()

    # --- price discovery --------------------------------------------------

    async def floors(self, collection: str) -> list[FloorQuote]:
        """Floor on every venue that answered, cheapest first.

        Venues are polled concurrently and independently: one slow or broken venue
        costs us its own result, never the whole quote.
        """
        results = await asyncio.gather(
            *(adapter.fetch_floor(collection) for adapter in self.adapters),
            return_exceptions=True,
        )
        quotes: list[FloorQuote] = []
        for adapter, result in zip(self.adapters, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("%s: floor lookup failed: %s", adapter.slug, result)
                continue
            if result is not None:
                quotes.append(result)
        quotes.sort(key=lambda quote: quote.floor_ton)
        return quotes

    async def spread(self, collection: str) -> SpreadQuote | None:
        """The cross-venue arbitrage picture, or None if fewer than two venues quoted."""
        quotes = await self.floors(collection)
        if len(quotes) < 2:
            return None
        cheapest, dearest = quotes[0], quotes[-1]
        return SpreadQuote(
            collection=collection,
            cheap_venue=cheapest.venue,
            cheap_ton=cheapest.floor_ton,
            rich_venue=dearest.venue,
            rich_ton=dearest.floor_ton,
        )

    # --- execution --------------------------------------------------------

    async def best_buy(
        self, collection: str, max_price_ton: float | None = None
    ) -> BuyQuote | None:
        """Cheapest executable listing across venues, referral attached.

        ``max_price_ton`` is the user's limit; listings above it are not returned at
        all rather than returned and rejected later, so a snipe never routes a user
        into a price they did not agree to.
        """
        listings_per_venue = await asyncio.gather(
            *(adapter.fetch_listings(collection, limit=25) for adapter in self.adapters),
            return_exceptions=True,
        )

        best_by_venue: dict[str, tuple[VenueAdapter, Listing]] = {}
        for adapter, result in zip(self.adapters, listings_per_venue, strict=True):
            if isinstance(result, BaseException):
                log.warning("%s: listing lookup failed: %s", adapter.slug, result)
                continue
            affordable = [
                listing
                for listing in result
                if max_price_ton is None or listing.price_ton <= max_price_ton
            ]
            if affordable:
                cheapest = min(affordable, key=lambda listing: listing.price_ton)
                best_by_venue[adapter.slug] = (adapter, cheapest)

        if not best_by_venue:
            return None

        ranked = sorted(best_by_venue.values(), key=lambda pair: pair[1].price_ton)
        adapter, listing = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else None

        fee_ton = round(listing.price_ton * self.settings.fee_rate, 4)
        return BuyQuote(
            listing=listing,
            venue=adapter.slug,
            price_ton=listing.price_ton,
            fee_ton=fee_ton,
            total_ton=round(listing.price_ton + fee_ton, 4),
            deep_link=adapter.buy_deep_link(listing),
            runner_up_venue=runner_up.venue if runner_up else "",
            runner_up_price_ton=runner_up.price_ton if runner_up else 0.0,
        )

    def build_payment_request(
        self, quote: BuyQuote, wallet_address: str, now: float | None = None
    ) -> dict:
        """A TON Connect transaction request for the user's wallet to sign.

        Returned as a plain dict and handed to the client — the Mini App passes it
        straight to ``tonConnectUI.sendTransaction``. This process constructs the
        request and never possesses the means to sign it.
        """
        if not wallet_address:
            raise ValueError("wallet_address is required to build a payment request")

        messages: list[dict] = []
        # The marketplace leg. Venue-specific payload construction belongs in the
        # adapter once each venue's on-chain sale contract is wired up; until then
        # the user completes the buy through the referral deep link.
        if self.settings.fee_enabled and quote.fee_ton > 0:
            messages.append(
                {
                    "address": self.settings.fee_wallet,
                    "amount": str(int(quote.fee_ton * 1_000_000_000)),
                    "payload": "",
                }
            )

        # A wallet rejects a transaction with no messages, and handing the user a
        # signature prompt that cannot succeed is worse than not offering one. With
        # no fee leg there is nothing on-chain for us to construct yet — the buy
        # completes through the referral deep link instead.
        if not messages:
            raise ValueError(
                "no on-chain legs to sign: enable an execution fee or use the "
                "referral deep link for this venue"
            )

        return {
            # Absolute UNIX seconds, which is what TON Connect expects — a relative
            # duration here reads as 1970 and every wallet rejects it as expired.
            # Five minutes: long enough for a human to read and sign, short enough
            # that a stale quote cannot execute after the floor has moved.
            "validUntil": int(now if now is not None else time.time()) + _QUOTE_TTL_SECONDS,
            "messages": messages,
            "meta": {
                "venue": quote.venue,
                "collection": quote.listing.collection,
                "listing_id": quote.listing.listing_id,
                "price_ton": quote.price_ton,
                "fee_ton": quote.fee_ton,
                "deep_link": quote.deep_link,
            },
        }
