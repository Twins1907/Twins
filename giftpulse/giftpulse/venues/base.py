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
import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

log = logging.getLogger(__name__)

# Status codes worth trying again. Everything else is a real answer — retrying a
# 404 just burns the rate budget we are trying to protect.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Backoff grows 0.5s, 1s, 2s... plus jitter, so a venue that rate-limits us does not
# get every collection retrying in lockstep.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 30.0


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

    def __init__(
        self,
        client: httpx.AsyncClient,
        referral_code: str = "",
        max_rps: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        self.client = client
        self.referral_code = referral_code
        self.max_retries = max_retries
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0
        # Set when the venue answers 401/403. Surfaced so the indexer can tell an
        # operator their credential died instead of silently serving fewer venues.
        self.auth_expired = False
        self._auth_warned = False

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
        return await self._request_json("GET", url, **kwargs)

    async def _post_json(self, url: str, **kwargs: object) -> object | None:
        return await self._request_json("POST", url, **kwargs)

    async def _request_json(self, method: str, url: str, **kwargs: object) -> object | None:
        """Paced, retrying request that never raises.

        Marketplaces rate-limit, wobble, and occasionally return HTML from a JSON
        endpoint. All of that is expected weather: the caller gets ``None`` and
        carries on with the venues that answered.
        """
        for attempt in range(self.max_retries + 1):
            await self._pace()
            last = attempt >= self.max_retries

            try:
                response = await self.client.request(method, url, **kwargs)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                if last:
                    log.warning("%s: transport error on %s: %s", self.slug, url, exc)
                    return None
                await self._backoff(attempt)
                continue

            status = response.status_code

            if status in (401, 403):
                self._note_auth_failure(status, url)
                return None

            if status in _RETRYABLE_STATUS:
                if last:
                    log.warning("%s: HTTP %s from %s, giving up", self.slug, status, url)
                    return None
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if status >= 400:
                log.warning("%s: HTTP %s from %s", self.slug, status, url)
                return None

            try:
                return response.json()
            except ValueError as exc:
                log.warning("%s: malformed JSON from %s: %s", self.slug, url, exc)
                return None

        return None

    async def _pace(self) -> None:
        """Hold requests to this venue under the configured rate.

        Enforced per adapter instance, which is why adapters are built once per
        process rather than once per poll — a limiter that resets every cycle is
        not a limiter.
        """
        if self._min_interval <= 0:
            return
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_request
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request = loop.time()

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
        if retry_after:
            # The venue told us how long to wait; that beats our guess.
            try:
                delay = max(delay, min(float(retry_after), _BACKOFF_MAX_SECONDS))
            except ValueError:
                pass
        await asyncio.sleep(delay + random.uniform(0, delay * 0.25))

    def _note_auth_failure(self, status: int, url: str) -> None:
        self.auth_expired = True
        if not self._auth_warned:
            log.warning(
                "%s: HTTP %s from %s — credentials rejected. This venue is now "
                "returning nothing until it is re-authenticated.",
                self.slug,
                status,
                url,
            )
            self._auth_warned = True


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
