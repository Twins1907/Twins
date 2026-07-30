"""Read queries shared by the bot and the Mini App API.

Both surfaces answer the same questions, so the SQL lives once here rather than
being written twice and drifting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from giftpulse.models import FloorSnapshot, RoutedTrade, SaleEvent


async def latest_floors(session: AsyncSession, collection: str) -> list[dict]:
    """Most recent floor per venue for one collection, cheapest first."""
    newest = (
        select(
            FloorSnapshot.venue.label("venue"),
            func.max(FloorSnapshot.captured_at).label("captured_at"),
        )
        .where(FloorSnapshot.collection == collection)
        .group_by(FloorSnapshot.venue)
        .subquery()
    )
    rows = await session.execute(
        select(FloorSnapshot)
        .join(
            newest,
            (FloorSnapshot.venue == newest.c.venue)
            & (FloorSnapshot.captured_at == newest.c.captured_at),
        )
        .where(FloorSnapshot.collection == collection)
        .order_by(FloorSnapshot.floor_ton.asc())
    )
    return [
        {
            "venue": row.venue,
            "floor_ton": row.floor_ton,
            "listing_count": row.listing_count,
            "captured_at": row.captured_at.isoformat(),
        }
        for row in rows.scalars()
    ]


async def floor_history(
    session: AsyncSession, collection: str, hours: int = 24, venue: str | None = None
) -> list[dict]:
    """Floor points over a window, for the Mini App chart."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    query = (
        select(FloorSnapshot)
        .where(FloorSnapshot.collection == collection, FloorSnapshot.captured_at >= since)
        .order_by(FloorSnapshot.captured_at.asc())
    )
    if venue:
        query = query.where(FloorSnapshot.venue == venue)

    rows = await session.execute(query)
    return [
        {
            "venue": row.venue,
            "floor_ton": row.floor_ton,
            "captured_at": row.captured_at.isoformat(),
        }
        for row in rows.scalars()
    ]


async def collection_overview(session: AsyncSession, collections: list[str]) -> list[dict]:
    """Cheapest venue and current spread for each collection — the scanner's main view."""
    overview: list[dict] = []
    for collection in collections:
        floors = await latest_floors(session, collection)
        if not floors:
            continue
        cheapest, dearest = floors[0], floors[-1]
        spread_pct = 0.0
        if cheapest["floor_ton"] > 0 and len(floors) > 1:
            spread_pct = round(
                (dearest["floor_ton"] - cheapest["floor_ton"]) / cheapest["floor_ton"] * 100, 2
            )
        overview.append(
            {
                "collection": collection,
                "best_venue": cheapest["venue"],
                "best_floor_ton": cheapest["floor_ton"],
                "spread_pct": spread_pct,
                "venues": floors,
            }
        )
    overview.sort(key=lambda item: item["spread_pct"], reverse=True)
    return overview


async def change_pct(session: AsyncSession, collection: str, hours: int = 24) -> float | None:
    """Percent change in best floor over a window. None if there is no baseline yet."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    old = await session.execute(
        select(func.min(FloorSnapshot.floor_ton)).where(
            FloorSnapshot.collection == collection,
            FloorSnapshot.captured_at <= since,
        )
    )
    baseline = old.scalar_one_or_none()
    if not baseline:
        return None

    current = await session.execute(
        select(func.min(FloorSnapshot.floor_ton))
        .where(FloorSnapshot.collection == collection)
        .order_by(FloorSnapshot.captured_at.desc())
        .limit(1)
    )
    now_value = current.scalar_one_or_none()
    if not now_value:
        return None
    return round((now_value - baseline) / baseline * 100, 2)


async def routed_volume_ton(session: AsyncSession, days: int = 30) -> dict:
    """Routed volume and fees — the metric the whole thesis is judged on."""
    since = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(
            func.coalesce(func.sum(RoutedTrade.price_ton), 0.0),
            func.coalesce(func.sum(RoutedTrade.fee_ton), 0.0),
            func.count(RoutedTrade.id),
        ).where(RoutedTrade.created_at >= since, RoutedTrade.status.in_(["built", "confirmed"]))
    )
    volume, fees, count = result.one()
    return {
        "days": days,
        "routed_volume_ton": round(float(volume), 2),
        "fee_ton": round(float(fees), 4),
        "trade_count": int(count),
    }


async def onchain_volume_ton(session: AsyncSession, collection: str, days: int = 7) -> float:
    """Observed sale volume, counting only settled on-chain trades.

    Internal-balance movements are excluded on purpose: including them is how the
    published venue figures end up inflated, and sizing off inflated numbers is one
    of the named risks in the plan.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(func.coalesce(func.sum(SaleEvent.price_ton), 0.0)).where(
            SaleEvent.collection == collection,
            SaleEvent.occurred_at >= since,
            SaleEvent.onchain.is_(True),
        )
    )
    return round(float(result.scalar_one()), 2)
