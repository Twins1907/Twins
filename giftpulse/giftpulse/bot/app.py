"""Bot process wiring."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher

from giftpulse.bot.handlers import router
from giftpulse.config import Settings, get_settings
from giftpulse.db import init_db
from giftpulse.venues.registry import close_shared_pool

log = logging.getLogger(__name__)


def build_bot(settings: Settings | None = None) -> Bot:
    settings = settings or get_settings()
    if not settings.bot_token:
        raise RuntimeError(
            "GIFTPULSE_BOT_TOKEN is not set. Create a bot with @BotFather and put the "
            "token in your .env — see .env.example."
        )
    return Bot(token=settings.bot_token)


async def run_bot(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    await init_db()

    bot = build_bot(settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    me = await bot.get_me()
    log.info("bot started as @%s", me.username)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await close_shared_pool()
        await bot.session.close()
