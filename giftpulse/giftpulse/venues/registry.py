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
                )
            )
        elif slug == "tonnel":
            adapters.append(TonnelAdapter(client, referral_code=settings.tonnel_referral))
        elif slug == "mrkt":
            adapters.append(MrktAdapter(client, referral_code=settings.mrkt_referral))

    return adapters


def _referral_for(slug: str, settings: Settings) -> str:
    return {
        "portals": settings.portals_referral,
        "tonnel": settings.tonnel_referral,
        "mrkt": settings.mrkt_referral,
    }.get(slug, "")
