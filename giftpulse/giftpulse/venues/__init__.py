"""Marketplace adapters. One module per venue, one contract in ``base``."""

from giftpulse.venues.base import FloorQuote, Listing, Sale, VenueAdapter
from giftpulse.venues.registry import build_adapters, build_client

__all__ = [
    "FloorQuote",
    "Listing",
    "Sale",
    "VenueAdapter",
    "build_adapters",
    "build_client",
]
