"""Deterministic synthetic venue.

Enabled by ``GIFTPULSE_MOCK=true``. This exists so the full pipeline — indexer,
alert rules, routing, bot, Mini App — runs end to end with no credentials and no
network. You can watch a floor break fire and a spread open up before you have ever
touched a marketplace API.

Prices are a seeded function of (venue, collection, tick), so two venues drift apart
in a stable, reproducible way and cross-venue spreads actually appear. Nothing here
is random at runtime: same inputs, same output, which is what makes the alert-rule
tests meaningful rather than flaky.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

from giftpulse.venues.base import Listing, Sale, VenueAdapter

# Real collection names, so demo output reads like the live market.
DEMO_COLLECTIONS = [
    "Plush Pepe",
    "Durov's Cap",
    "Precious Peach",
    "Heroic Helmet",
    "Toy Bear",
    "Astral Shard",
    "Eternal Rose",
    "Swiss Watch",
]

# Rough baseline floors in TON, only so the numbers are plausible in a demo.
_BASELINES = {
    "Plush Pepe": 4200.0,
    "Durov's Cap": 2600.0,
    "Precious Peach": 1450.0,
    "Heroic Helmet": 380.0,
    "Toy Bear": 44.0,
    "Astral Shard": 92.0,
    "Eternal Rose": 26.0,
    "Swiss Watch": 118.0,
}

_MODELS = ["Classic", "Frosted", "Golden", "Neon", "Obsidian"]
_BACKDROPS = ["Midnight", "Coral", "Ivory", "Emerald", "Crimson"]

# Sale timestamps are anchored to process start rather than read from the clock on
# each call. A sale's identity is (venue, gift_id, occurred_at), so re-generating
# the same synthetic sale with a fresh `now` would make it look like a new trade and
# the indexer would double-count it every cycle.
_EPOCH = datetime.now(UTC)


def _seed(*parts: object) -> float:
    """Stable float in [0, 1) derived from the given parts."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


class MockAdapter(VenueAdapter):
    """Synthetic venue that mimics one real marketplace's pricing behaviour."""

    def __init__(
        self,
        client,  # noqa: ANN001 - unused, kept for interface parity
        referral_code: str = "",
        slug: str = "mock",
        display_name: str = "Mock",
        tick: int = 0,
        drift: float = 0.0,
    ) -> None:
        # max_rps=0 disables pacing: there is no venue on the other end of this to
        # be polite to, and throttling it would only slow the tests down.
        super().__init__(client, referral_code, max_rps=0.0)
        self.slug = slug
        self.display_name = display_name
        # Advanced by the caller to simulate the passage of time.
        self.tick = tick
        # Per-venue systematic offset — this is what creates arbitrage spreads.
        self.drift = drift

    def _floor_for(self, collection: str) -> float:
        base = _BASELINES.get(collection, 100.0 + 900.0 * _seed(collection))
        # Slow sine wave plus a per-tick jitter, so floors move but stay believable.
        wave = math.sin((self.tick + _seed(collection) * 10) / 3.0) * 0.06
        jitter = (_seed(self.slug, collection, self.tick) - 0.5) * 0.04
        return round(base * (1 + self.drift + wave + jitter), 2)

    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        if collection not in _BASELINES and collection not in DEMO_COLLECTIONS:
            return []
        floor = self._floor_for(collection)
        now = datetime.now(UTC)

        listings: list[Listing] = []
        for index in range(min(limit, 12)):
            # Each successive listing sits a little above the floor.
            step = 1 + (index * 0.035) + _seed(self.slug, collection, self.tick, index) * 0.02
            price = round(floor * step, 2)
            listing_id = f"{self.slug}-{abs(hash((collection, self.tick, index))) % 10**8}"
            listings.append(
                Listing(
                    venue=self.slug,
                    collection=collection,
                    listing_id=listing_id,
                    gift_id=str(1000 + index),
                    price_ton=price,
                    model=_MODELS[index % len(_MODELS)],
                    backdrop=_BACKDROPS[index % len(_BACKDROPS)],
                    url=f"https://t.me/{self.slug}/app?startapp={listing_id}",
                    seller=f"EQ{_seed(self.slug, index):.16f}".replace(".", "")[:48],
                    captured_at=now,
                )
            )
        return listings

    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        if collection not in _BASELINES and collection not in DEMO_COLLECTIONS:
            return []
        floor = self._floor_for(collection)

        sales: list[Sale] = []
        for index in range(min(limit, 8)):
            roll = _seed(self.slug, collection, self.tick, "sale", index)
            # One sale in eight is a deliberate outlier, so whale detection has
            # something to find in demo mode.
            multiplier = 3.2 if index == 0 and roll > 0.55 else 0.95 + roll * 0.3
            sales.append(
                Sale(
                    venue=self.slug,
                    collection=collection,
                    gift_id=f"{2000 + index}",
                    price_ton=round(floor * multiplier, 2),
                    buyer=f"EQbuyer{index}{self.slug}",
                    seller=f"EQseller{index}{self.slug}",
                    onchain=roll > 0.4,
                    occurred_at=_EPOCH - timedelta(minutes=(self.tick * 8 + index) * 7),
                )
            )
        return sales
