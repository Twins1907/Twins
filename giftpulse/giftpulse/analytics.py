"""Read queries shared by the bot and the Mini App API.

Both surfaces answer the same questions, so the SQL lives once here rather than
being written twice and drifting.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, cast, func, select
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


def _epoch_seconds(session: AsyncSession):  # noqa: ANN201
    """Portable "captured_at as a UNIX timestamp" expression.

    SQLite and Postgres spell this differently and there is no common syntax, so the
    dialect branch lives here once instead of in every caller.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return cast(func.strftime("%s", FloorSnapshot.captured_at), Integer)
    return cast(func.extract("epoch", FloorSnapshot.captured_at), Integer)


async def floor_history(
    session: AsyncSession,
    collection: str,
    hours: int = 24,
    venue: str | None = None,
    max_points: int = 180,
) -> list[dict]:
    """Floor points over a window, for the Mini App chart.

    Downsampled in the database. A 60-second poll across three venues produces
    ~4,300 rows for a day and ~130,000 for a month; a chart a few hundred pixels
    wide cannot draw any of that, so sending it is bandwidth and parse time spent to
    render the same line.

    ``max_points`` is per venue — a three-venue chart returns up to three times as
    many rows, because each venue draws its own line.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    # Buckets align to absolute epoch rather than to the window start, so that the
    # same instant lands in the same bucket on every request and the chart does not
    # shimmer as time passes. The cost is that a window can straddle one more
    # boundary than a naive division suggests, so divide by one fewer interval to
    # keep max_points an actual maximum.
    intervals = max(1, max_points - 1)
    bucket_seconds = max(60, math.ceil(hours * 3600 / intervals))
    bucket = cast(_epoch_seconds(session) / bucket_seconds, Integer).label("bucket")

    query = (
        select(
            FloorSnapshot.venue,
            bucket,
            func.min(FloorSnapshot.floor_ton).label("floor_ton"),
            func.min(FloorSnapshot.captured_at).label("captured_at"),
        )
        .where(FloorSnapshot.collection == collection, FloorSnapshot.captured_at >= since)
        .group_by(FloorSnapshot.venue, bucket)
        .order_by(func.min(FloorSnapshot.captured_at).asc())
    )
    if venue:
        query = query.where(FloorSnapshot.venue == venue)

    rows = await session.execute(query)
    return [
        {
            "venue": row.venue,
            "floor_ton": round(float(row.floor_ton), 2),
            "captured_at": _isoformat(row.captured_at),
        }
        for row in rows
    ]


def _isoformat(moment: datetime | str) -> str:
    """Timestamps come back as datetimes on Postgres and strings on SQLite."""
    return moment.isoformat() if isinstance(moment, datetime) else str(moment)


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


# How wide a window either side of the lookback point counts as "then". Wide enough
# to survive a missed poll, narrow enough that it is still a point in time.
_BASELINE_WINDOW = timedelta(minutes=30)

# Snapshots this close to the newest one are treated as "now" — the three venues in
# a single cycle do not land on the same millisecond.
_CURRENT_WINDOW = timedelta(minutes=5)


async def change_pct(session: AsyncSession, collection: str, hours: int = 24) -> float | None:
    """Percent change in best floor over a window. None if there is no baseline yet.

    Both ends are the cheapest floor *at a point in time*. Aggregating without a
    time bound — which is what an unbounded ``min()`` does — compares the all-time
    low against the all-time low and reports a change of zero forever.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)

    baseline_result = await session.execute(
        select(func.min(FloorSnapshot.floor_ton)).where(
            FloorSnapshot.collection == collection,
            FloorSnapshot.captured_at >= since - _BASELINE_WINDOW,
            FloorSnapshot.captured_at <= since + _BASELINE_WINDOW,
        )
    )
    baseline = baseline_result.scalar_one_or_none()
    if not baseline:
        return None

    latest_result = await session.execute(
        select(func.max(FloorSnapshot.captured_at)).where(
            FloorSnapshot.collection == collection
        )
    )
    latest_at = latest_result.scalar_one_or_none()
    if latest_at is None:
        return None
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=UTC)

    current_result = await session.execute(
        select(func.min(FloorSnapshot.floor_ton)).where(
            FloorSnapshot.collection == collection,
            FloorSnapshot.captured_at >= latest_at - _CURRENT_WINDOW,
        )
    )
    now_value = current_result.scalar_one_or_none()
    if not now_value:
        return None
    return round((now_value - baseline) / baseline * 100, 2)


async def routed_volume_ton(session: AsyncSession, days: int = 30) -> dict:
    """Routed volume and fees — the metric the whole thesis is judged on.

    Volume counts settled trades only. A quote is a price lookup: it costs nothing,
    commits nobody, and earns nothing. Counting quotes as volume would mean anyone
    opening the scanner inflates the number the product is judged on, which is the
    quickest way to be wrong about whether this business works.

    Quote counts are still reported, because quote→confirmed is the conversion rate
    that says whether the sign flow is working.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    settled = await session.execute(
        select(
            func.coalesce(func.sum(RoutedTrade.price_ton), 0.0),
            func.coalesce(func.sum(RoutedTrade.fee_ton), 0.0),
            func.count(RoutedTrade.id),
        ).where(
            RoutedTrade.created_at >= since,
            RoutedTrade.status.in_(RoutedTrade.REVENUE_STATUSES),
        )
    )
    volume, fees, trades = settled.one()

    funnel = await session.execute(
        select(RoutedTrade.status, func.count(RoutedTrade.id))
        .where(RoutedTrade.created_at >= since)
        .group_by(RoutedTrade.status)
    )
    by_status = {status: int(count) for status, count in funnel}
    quotes = by_status.get(RoutedTrade.QUOTED, 0)

    return {
        "days": days,
        "routed_volume_ton": round(float(volume), 2),
        "fee_ton": round(float(fees), 4),
        "trade_count": int(trades),
        "quote_count": quotes,
        "by_status": by_status,
        "quote_to_trade_pct": (
            round(int(trades) / quotes * 100, 2) if quotes else None
        ),
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
