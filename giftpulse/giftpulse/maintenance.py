"""Housekeeping the indexer runs alongside polling.

The snapshot table is append-only by design — every alert rule is a delta question
and you cannot ask one of a table that only remembers now. The cost of that design
is unbounded growth: eight collections across three venues at a 60-second interval
is roughly 35,000 rows a day, before sales and alert history. Left alone, the chart
queries degrade and the disk fills, both of them slowly enough that nobody notices
until it matters.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from giftpulse.models import AlertLog, FloorSnapshot, SaleEvent

log = logging.getLogger(__name__)

# Tables pruned on the same retention clock. Alert history is kept as long as the
# market data so alert->trade attribution can still be reconstructed over the
# window the product is judged on.
_PRUNABLE = (
    (FloorSnapshot, FloorSnapshot.captured_at),
    (SaleEvent, SaleEvent.occurred_at),
    (AlertLog, AlertLog.sent_at),
)


async def prune_old_data(session: AsyncSession, retention_days: int) -> dict[str, int]:
    """Delete rows older than the retention window. Returns per-table counts."""
    if retention_days <= 0:
        return {}

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    removed: dict[str, int] = {}

    for model, timestamp_column in _PRUNABLE:
        result = await session.execute(delete(model).where(timestamp_column < cutoff))
        count = result.rowcount or 0
        if count:
            removed[model.__tablename__] = count

    if removed:
        log.info(
            "pruned rows older than %d days: %s",
            retention_days,
            ", ".join(f"{table}={count}" for table, count in removed.items()),
        )
    return removed


async def table_sizes(session: AsyncSession) -> dict[str, int]:
    """Row counts for the growing tables, for /api/health and capacity planning."""
    sizes: dict[str, int] = {}
    for model, _ in _PRUNABLE:
        result = await session.execute(select(func.count()).select_from(model))
        sizes[model.__tablename__] = int(result.scalar_one())
    return sizes
