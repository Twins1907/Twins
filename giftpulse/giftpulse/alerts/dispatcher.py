"""Delivers alerts to the channel and to individual users.

Two jobs beyond "send a message": cooldown (never post the same event twice) and
rate limiting (Telegram will drop us if we burst). Both live here so the rules stay
pure and the indexer stays ignorant of delivery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from giftpulse.alerts.rules import Alert, format_for_telegram
from giftpulse.config import Settings, get_settings
from giftpulse.models import AlertLog

log = logging.getLogger(__name__)

# Telegram allows ~30 messages/second to distinct chats and ~20/minute to one
# channel. Sit well under both; a dropped alert is worse than a late one.
_MIN_INTERVAL_SECONDS = 3.5


class AlertDispatcher:
    def __init__(self, bot=None, settings: Settings | None = None) -> None:  # noqa: ANN001
        # ``bot`` is an aiogram Bot, or None to run the pipeline without sending
        # (used by tests and by `giftpulse indexer --dry-run`).
        self.bot = bot
        self.settings = settings or get_settings()
        self.bot_username = ""
        self._last_send = 0.0
        self._lock = asyncio.Lock()

    async def dispatch(self, session: AsyncSession, alerts: list[Alert]) -> list[Alert]:
        """Send the alerts that survive cooldown. Returns those actually sent."""
        sent: list[Alert] = []
        for alert in alerts:
            if await self._on_cooldown(session, alert):
                log.debug("suppressed (cooldown): %s", alert.dedupe_key)
                continue

            delivered = await self._deliver(alert)
            if not delivered:
                continue

            session.add(
                AlertLog(
                    kind=alert.kind,
                    collection=alert.collection,
                    dedupe_key=alert.dedupe_key,
                    body=alert.body[:2048],
                )
            )
            sent.append(alert)
        return sent

    async def _deliver(self, alert: Alert) -> bool:
        text = format_for_telegram(alert, self.bot_username)

        # watch_hit is a personal signal — it goes to the user who set the target,
        # never to the public channel where it would be meaningless noise.
        target = (
            alert.metadata.get("telegram_id")
            if alert.kind == "watch_hit"
            else self.settings.alert_channel
        )
        if not target:
            log.info("no delivery target configured, alert not sent: %s", alert.title)
            return False

        if self.bot is None:
            log.info("[dry-run] %s -> %s", alert.title, target)
            return True

        await self._throttle()
        try:
            await self.bot.send_message(
                chat_id=target,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as exc:  # aiogram raises a wide family of transport errors
            log.warning("failed to deliver alert %s: %s", alert.dedupe_key, exc)
            return False

    async def _throttle(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_send
            if elapsed < _MIN_INTERVAL_SECONDS:
                await asyncio.sleep(_MIN_INTERVAL_SECONDS - elapsed)
            self._last_send = loop.time()

    async def _on_cooldown(self, session: AsyncSession, alert: Alert) -> bool:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.alert_cooldown_seconds)
        result = await session.execute(
            select(AlertLog.id).where(
                AlertLog.dedupe_key == alert.dedupe_key,
                AlertLog.sent_at >= cutoff,
            )
        )
        return result.scalar_one_or_none() is not None
