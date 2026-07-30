"""Builds the live set of venue adapters from configuration."""

from __future__ import annotations

import httpx

from giftpulse.config import Settings, get_settings
from giftpulse.venues.base import VenueAdapter
from giftpulse.venues.mock import MockAdapter
from giftpulse.venues.mrkt import MrktAdapter
from giftpulse.venues.portals import PortalsAdapter
from giftpulse.venues.tonnel import TonnelAdapter

# Per-venue systematic price offsets used only in mock mode, chosen so that
# cross-venue spreads are visible immediately in a demo.
_MOCK_DRIFT = {"portals": 0.0, "tonnel": -0.045, "mrkt": 0.03}


def build_client(settings: Settings | None = None) -> httpx.AsyncClient:
    settings = settings or get_settings()
    return httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": "GiftPulse/0.1 (+https://github.com/Twins1907/giftpulse)"},
        follow_redirects=True,
        # Keep connections warm between polls. Renegotiating TLS with three venues
        # every 60 seconds is latency paid for nothing.
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
    )


def build_adapters(
    client: httpx.AsyncClient,
    settings: Settings | None = None,
    tick: int = 0,
) -> list[VenueAdapter]:
    """Adapters for every enabled venue.

    In mock mode each enabled slug is served by a synthetic adapter that keeps the
    real venue's name, so downstream code and demo output are identical either way.
    """
    settings = settings or get_settings()
    adapters: list[VenueAdapter] = []
    pacing = {"max_rps": settings.venue_max_rps, "max_retries": settings.venue_max_retries}

    for slug in settings.enabled_venues:
        if settings.mock:
            adapters.append(
                MockAdapter(
                    client,
                    referral_code=_referral_for(slug, settings),
                    slug=slug,
                    display_name=slug.capitalize(),
                    tick=tick,
                    drift=_MOCK_DRIFT.get(slug, 0.0),
                )
            )
        elif slug == "portals":
            adapters.append(
                PortalsAdapter(
                    client,
                    referral_code=settings.portals_referral,
                    auth_data=settings.portals_auth_data,
                    **pacing,
                )
            )
        elif slug == "tonnel":
            adapters.append(
                TonnelAdapter(client, referral_code=settings.tonnel_referral, **pacing)
            )
        elif slug == "mrkt":
            adapters.append(MrktAdapter(client, referral_code=settings.mrkt_referral, **pacing))

    return adapters


class VenuePool:
    """Owns the HTTP client and adapter instances for the lifetime of a process.

    Adapters carry state that only means anything if it survives: the per-venue rate
    limiter, the "this credential is dead" flag, and the once-per-process warning
    guards. Rebuilding them on every poll — which is what happened before — reset all
    three each cycle, so the rate limit never actually bound and an expired
    credential re-warned once a minute forever.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = build_client(self.settings)
        self.adapters = build_adapters(self.client, self.settings)

    def adapters_for_tick(self, tick: int) -> list[VenueAdapter]:
        """The pool's adapters, with mock clocks advanced to ``tick``.

        Real adapters have no notion of a tick; only the synthetic venue does, and it
        needs one to make prices move in a demo.
        """
        for adapter in self.adapters:
            if isinstance(adapter, MockAdapter):
                adapter.tick = tick
        return self.adapters

    def expired_credentials(self) -> list[str]:
        """Venues that have rejected our credentials since the flag was last cleared."""
        return [adapter.slug for adapter in self.adapters if adapter.auth_expired]

    def clear_credential_flags(self) -> None:
        for adapter in self.adapters:
            adapter.auth_expired = False

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> VenuePool:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


_shared_pool: VenuePool | None = None


def get_shared_pool(settings: Settings | None = None) -> VenuePool:
    """Process-wide pool for callers with nowhere to hang one.

    The bot's handlers are the case: aiogram gives a handler no application object,
    and building a client per button press throws away connection reuse and resets
    the rate limiter on every tap. The API uses ``app.state`` instead, which has a
    proper lifespan to close it.
    """
    global _shared_pool
    if _shared_pool is None:
        _shared_pool = VenuePool(settings)
    return _shared_pool


async def close_shared_pool() -> None:
    global _shared_pool
    if _shared_pool is not None:
        await _shared_pool.aclose()
        _shared_pool = None


def _referral_for(slug: str, settings: Settings) -> str:
    return {
        "portals": settings.portals_referral,
        "tonnel": settings.tonnel_referral,
        "mrkt": settings.mrkt_referral,
    }.get(slug, "")
