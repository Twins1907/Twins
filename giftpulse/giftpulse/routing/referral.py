"""Referral plumbing.

Two directions, and they are not the same thing:

  * **Outbound** — our code on marketplace links, so their commission partly flows
    back to us. This is the day-one revenue line and needs no custody, no fee
    contract, and no user trust beyond clicking a link.
  * **Inbound** — each user's own code for inviting others, mirroring the mechanic
    the marketplaces already run downstream.
"""

from __future__ import annotations

import hashlib

from giftpulse.config import Settings, get_settings


def user_referral_code(telegram_id: int) -> str:
    """Short, stable, non-sequential code for a user.

    Hashed rather than derived from the Telegram id directly so that a shared
    referral link does not leak the referrer's account id.
    """
    digest = hashlib.sha256(f"giftpulse:{telegram_id}".encode()).hexdigest()
    return digest[:8]


def user_referral_link(bot_username: str, telegram_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_referral_code(telegram_id)}"


def parse_start_payload(payload: str) -> dict[str, str]:
    """Decode a ``/start`` deep-link payload.

    Understands ``ref_<code>`` for referrals and ``c_<collection>`` for alert
    links that should open straight onto a collection.
    """
    result: dict[str, str] = {}
    if not payload:
        return result
    for part in payload.split("__"):
        if part.startswith("ref_"):
            result["referrer"] = part[4:]
        elif part.startswith("c_"):
            result["collection"] = part[2:].replace("_", " ")
    return result


def venue_referral(slug: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return {
        "portals": settings.portals_referral,
        "tonnel": settings.tonnel_referral,
        "mrkt": settings.mrkt_referral,
    }.get(slug, "")
