"""Venue adapter contract.

Every marketplace sits behind this interface. Portals rotates its auth blob, Tonnel
reshapes its JSON, MRKT renames a field — each of those is a one-file change here and
nothing upstream notices. The routing engine, indexer, and alert rules only ever see
the normalized types below.

Adapters must not raise on transport failure. A venue being down is an expected
weather condition, not an exception: return empty and let the caller carry on with
the venues that answered.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Listing:
    """A single gift offered for sale, normalized across venues."""

    venue: str
    collection: str
    listing_id: str
    price_ton: float
    gift_id: str = ""
    model: str = ""
    backdrop: str = ""
    url: str = ""
    seller: str = ""
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def with_referral(self, code: str) -> str:
        """Listing URL carrying our referral code, or the bare URL if none is set."""
        if not code or not self.url:
            return self.url
        joiner = "&" if "?" in self.url else "?"
        return f"{self.url}{joiner}ref={code}"


@dataclass(frozen=True, slots=True)
class Sale:
    """A completed sale as reported by a venue."""

    venue: str
    collection: str
    gift_id: str
    price_ton: float
    buyer: str = ""
    seller: str = ""
    # Venues report internal-balance movements alongside settled trades. Only
    # entries we can tie to a chain event get True; volume math must respect it.
    onchain: bool = False
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class FloorQuote:
    venue: str
    collection: str
    floor_ton: float
    listing_count: int
    listing_id: str = ""
    url: str = ""


class VenueAdapter(abc.ABC):
    """Base class for a marketplace integration."""

    slug: str = ""
    display_name: str = ""
    base_url: str = ""

    def __init__(self, client: httpx.AsyncClient, referral_code: str = "") -> None:
        self.client = client
        self.referral_code = referral_code

    # --- required surface -------------------------------------------------

    @abc.abstractmethod
    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        """Cheapest-first active listings for a collection."""

    @abc.abstractmethod
    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        """Most recent completed sales for a collection."""

    # --- derived ----------------------------------------------------------

    async def fetch_floor(self, collection: str) -> FloorQuote | None:
        listings = await self.fetch_listings(collection, limit=50)
        if not listings:
            return None
        cheapest = min(listings, key=lambda item: item.price_ton)
        return FloorQuote(
            venue=self.slug,
            collection=collection,
            floor_ton=cheapest.price_ton,
            listing_count=len(listings),
            listing_id=cheapest.listing_id,
            url=cheapest.with_referral(self.referral_code),
        )

    def buy_deep_link(self, listing: Listing) -> str:
        """Where to send a user to complete this purchase, referral attached."""
        return listing.with_referral(self.referral_code)

    # --- transport helper -------------------------------------------------

    async def _get_json(self, url: str, **kwargs: object) -> object | None:
        """GET returning parsed JSON, or None on any transport/decode failure."""
        try:
            response = await self.client.get(url, **kwargs)  # type: ignore[arg-type]
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            log.warning("%s: HTTP %s from %s", self.slug, exc.response.status_code, url)
        except httpx.HTTPError as exc:
            log.warning("%s: transport error on %s: %s", self.slug, url, exc)
        except ValueError as exc:
            log.warning("%s: malformed JSON from %s: %s", self.slug, url, exc)
        return None

    async def _post_json(self, url: str, **kwargs: object) -> object | None:
        try:
            response = await self.client.post(url, **kwargs)  # type: ignore[arg-type]
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            log.warning("%s: HTTP %s from %s", self.slug, exc.response.status_code, url)
        except httpx.HTTPError as exc:
            log.warning("%s: transport error on %s: %s", self.slug, url, exc)
        except ValueError as exc:
            log.warning("%s: malformed JSON from %s: %s", self.slug, url, exc)
        return None


def to_ton(value: object, divisor: float = 1.0) -> float | None:
    """Coerce a venue's price field to TON, or None if it isn't a usable number.

    Venues variously send floats, decimal strings, and integer nanoton. ``divisor``
    lets an adapter say which it is without every call site repeating the maths.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount / divisor
