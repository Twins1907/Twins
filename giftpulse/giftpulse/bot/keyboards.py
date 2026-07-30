"""Inline keyboards."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from giftpulse.config import get_settings


def main_menu() -> InlineKeyboardMarkup:
    settings = get_settings()
    rows = [
        [InlineKeyboardButton(text="🔍 Scan floors", callback_data="scan")],
        [
            InlineKeyboardButton(text="⚡ Best spreads", callback_data="spreads"),
            InlineKeyboardButton(text="🎯 My watches", callback_data="watches"),
        ],
        [InlineKeyboardButton(text="🔗 Invite & earn", callback_data="referral")],
    ]
    # The Mini App button only works over HTTPS; offering it on a localhost dev
    # setup produces a confusing Telegram-side error instead of a clear absence.
    if settings.public_url.startswith("https://"):
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text="📊 Open GiftPulse",
                    web_app=WebAppInfo(url=settings.public_url),
                )
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def collection_menu(collections: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for index in range(0, len(collections), 2):
        chunk = collections[index : index + 2]
        rows.append(
            [
                InlineKeyboardButton(text=name, callback_data=f"floor:{name}")
                for name in chunk
            ]
        )
    rows.append([InlineKeyboardButton(text="← Back", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def buy_menu(collection: str, deep_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Buy cheapest", url=deep_link)],
            [
                InlineKeyboardButton(text="🎯 Watch", callback_data=f"watch:{collection}"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data=f"floor:{collection}"),
            ],
            [InlineKeyboardButton(text="← Back", callback_data="scan")],
        ]
    )
