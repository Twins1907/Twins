"""Liveness tracking for the background loops.

A dead indexer and a quiet market produce the same observable behaviour: the channel
stops posting. The difference matters enormously and nothing else in the system can
tell them apart, so the indexer records every cycle outcome here and the API reports
it. An external uptime monitor watching ``/api/health`` turns that into a page.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from giftpulse.models import ServiceHeartbeat

log = logging.getLogger(__name__)

INDEXER = "indexer"


async def _upsert(session: AsyncSession, name: str) -> ServiceHeartbeat:
    result = await session.execute(
        select(ServiceHeartbeat).where(ServiceHeartbeat.name == name)
    )
    heartbeat = result.scalar_one_or_none()
    if heartbeat is None:
        heartbeat = ServiceHeartbeat(name=name)
        session.add(heartbeat)
    return heartbeat


async def record_success(session: AsyncSession, name: str, detail: str = "") -> None:
    heartbeat = await _upsert(session, name)
    heartbeat.last_success_at = datetime.now(UTC)
    heartbeat.consecutive_failures = 0
    heartbeat.detail = detail[:512]


async def record_failure(session: AsyncSession, name: str, detail: str) -> int:
    """Record a failed cycle. Returns the new consecutive-failure count."""
    heartbeat = await _upsert(session, name)
    heartbeat.last_failure_at = datetime.now(UTC)
    heartbeat.consecutive_failures = (heartbeat.consecutive_failures or 0) + 1
    heartbeat.detail = detail[:512]
    return heartbeat.consecutive_failures


async def read(session: AsyncSession, name: str = INDEXER) -> dict | None:
    result = await session.execute(
        select(ServiceHeartbeat).where(ServiceHeartbeat.name == name)
    )
    heartbeat = result.scalar_one_or_none()
    if heartbeat is None:
        return None

    last_success = heartbeat.last_success_at
    age_seconds: float | None = None
    if last_success is not None:
        # SQLite returns naive datetimes from a timezone-aware column.
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=UTC)
        age_seconds = round((datetime.now(UTC) - last_success).total_seconds(), 1)

    return {
        "name": heartbeat.name,
        "last_success_at": last_success.isoformat() if last_success else None,
        "seconds_since_success": age_seconds,
        "consecutive_failures": heartbeat.consecutive_failures or 0,
        "detail": heartbeat.detail or "",
    }


def status_for(heartbeat: dict | None, stall_seconds: int) -> str:
    """``ok``, ``degraded``, or ``starting`` — what an uptime monitor should act on."""
    if heartbeat is None or heartbeat["last_success_at"] is None:
        return "starting"
    age = heartbeat["seconds_since_success"]
    if age is not None and age > stall_seconds:
        return "degraded"
    if heartbeat["consecutive_failures"] > 0:
        return "degraded"
    return "ok"
