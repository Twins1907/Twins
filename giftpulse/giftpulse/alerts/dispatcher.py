"""Delivers alerts to the channel and to individual users.

Three jobs beyond "send a message": cooldown (never post the same event twice), an
hourly budget (never let one volatile hour empty the channel), and rate limiting
(Telegram drops us if we burst). All three live here so the rules stay pure and the
indexer stays ignorant of delivery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from giftpulse.alerts.rules import Alert, format_for_telegram
from giftpulse.config import Settings, get_settings
from giftpulse.models import AlertLog

log = logging.getLogger(__name__)

# Telegram allows ~30 messages/second to distinct chats and ~20/minute to one
# channel. Sit well under both; a dropped alert is worse than a late one.
_MIN_INTERVAL_SECONDS = 3.5

# When the hour's budget is tight, these go first. A cross-venue spread or a floor
# break is something a trader can act on in the next minute; a whale print and a
# supply burst are context. Ranking by actionability is what keeps a capped channel
# useful rather than merely quieter.
_PRIORITY = {
    "watch_hit": 0,
    "spread": 1,
    "floor_break": 2,
    "whale": 3,
    "supply_burst": 4,
}

# A personal DM is not channel noise and must never be squeezed out by a busy
# market — the user explicitly asked for this one price.
_EXEMPT_FROM_BUDGET = frozenset({"watch_hit"})


def rank_alerts(alerts: list[Alert]) -> list[Alert]:
    """Most actionable first, largest move first within a kind."""
    return sorted(
        alerts,
        key=lambda alert: (_PRIORITY.get(alert.kind, 9), -abs(alert.price_ton)),
    )


class AlertDispatcher:
    def __init__(self, bot=None, settings: Settings | None = None) -> None:  # noqa: ANN001
        # ``bot`` is an aiogram Bot, or None to run the pipeline without sending
        # (used by tests and by `giftpulse indexer --dry-run`).
        self.bot = bot
        self.settings = settings or get_settings()
        self.bot_username = ""
        self._last_send = 0.0
        self._lock = asyncio.Lock()
        # Counts alerts suppressed by the hourly budget, so the operator can see
        # the channel is being capped rather than the market being quiet.
        self.budget_suppressed = 0

    async def dispatch(self, session: AsyncSession, alerts: list[Alert]) -> list[Alert]:
        """Send the alerts that survive cooldown and the hourly budget.

        Returns those actually sent, most actionable first.
        """
        sent: list[Alert] = []
        budget = await self._remaining_budget(session)

        for alert in rank_alerts(alerts):
            counts_against_budget = alert.kind not in _EXEMPT_FROM_BUDGET

            if counts_against_budget and budget <= 0:
                self.budget_suppressed += 1
                log.info(
                    "hourly channel budget spent, holding: %s", alert.dedupe_key
                )
                continue

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
            if counts_against_budget:
                budget -= 1

            # Commit as each alert lands rather than at the end of the batch. A
            # crash halfway through a dispatch would otherwise roll back the log
            # for alerts that were already delivered, and every one of them would
            # be sent again on restart.
            await session.commit()

        return sent

    async def _remaining_budget(self, session: AsyncSession) -> int:
        """Channel posts still allowed this hour.

        Cooldown already stops the *same* event repeating. This stops thirty
        *distinct* events from a single market-wide move arriving back to back —
        which is what actually costs subscribers, and what a per-event cooldown
        cannot see.
        """
        cap = self.settings.alert_max_per_hour
        if cap <= 0:
            return 1_000_000

        since = datetime.now(UTC) - timedelta(hours=1)
        result = await session.execute(
            select(func.count(AlertLog.id)).where(
                AlertLog.sent_at >= since,
                AlertLog.kind.not_in(tuple(_EXEMPT_FROM_BUDGET)),
            )
        )
        return max(0, cap - int(result.scalar_one()))

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
        return result.scalars().first() is not None

    async def notify_admin(self, text: str) -> bool:
        """Send an operational notice to the admin chat.

        Deliberately not routed through cooldown or the budget: if the indexer has
        stalled or a credential died, that message must not be dropped because the
        market happened to be busy.
        """
        target = self.settings.admin_chat_id
        if not target:
            log.warning("admin notice (no admin chat configured): %s", text)
            return False
        if self.bot is None:
            log.info("[dry-run] admin notice -> %s: %s", target, text)
            return True
        try:
            await self.bot.send_message(chat_id=target, text=text, parse_mode="HTML")
            return True
        except Exception as exc:
            log.warning("failed to deliver admin notice: %s", exc)
            return False
