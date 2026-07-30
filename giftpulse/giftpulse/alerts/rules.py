"""Alert rules.

Every rule is a pure function: snapshots and thresholds in, ``Alert`` or ``None``
out. No database handle, no network, no clock reads beyond what is passed in. That
makes each rule directly testable, and it means the dispatcher can be swapped or
replayed over historical data without touching the logic that decides what matters.

The rules are the product. If they fire on noise, users mute the channel and the
funnel dies; if they fire late, the trade is gone. Thresholds are configuration,
not constants, for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from giftpulse.routing.engine import SpreadQuote
from giftpulse.venues.base import FloorQuote, Sale


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str
    collection: str
    title: str
    body: str
    # Suppresses repeats of the same event within the cooldown window.
    dedupe_key: str
    venue: str = ""
    price_ton: float = 0.0
    url: str = ""
    metadata: dict = field(default_factory=dict)


def floor_break(
    collection: str,
    previous_floor: float,
    current: FloorQuote,
    threshold_pct: float,
) -> Alert | None:
    """Fires when a floor drops by more than ``threshold_pct``.

    Downward only. A floor rising is not an execution opportunity, and mixing the two
    into one "floor moved" alert is how a signal channel becomes noise.
    """
    if previous_floor <= 0 or current.floor_ton <= 0:
        return None

    change_pct = (current.floor_ton - previous_floor) / previous_floor * 100
    if change_pct > -threshold_pct:
        return None

    drop = abs(round(change_pct, 2))
    return Alert(
        kind="floor_break",
        collection=collection,
        title=f"📉 {collection} floor −{drop}%",
        body=(
            f"{collection} floor fell {drop}% on {current.venue}: "
            f"{previous_floor:.2f} → {current.floor_ton:.2f} TON "
            f"({current.listing_count} listed)"
        ),
        dedupe_key=f"floor_break:{collection}:{current.venue}:{round(current.floor_ton, 1)}",
        venue=current.venue,
        price_ton=current.floor_ton,
        url=current.url,
        metadata={"previous_floor": previous_floor, "change_pct": round(change_pct, 2)},
    )


def spread_alert(spread: SpreadQuote, threshold_pct: float) -> Alert | None:
    """Fires when the same collection is materially cheaper on one venue than another."""
    if spread.spread_pct < threshold_pct:
        return None

    return Alert(
        kind="spread",
        collection=spread.collection,
        title=f"⚡ {spread.collection} {spread.spread_pct}% cross-venue spread",
        body=(
            f"{spread.collection} is {spread.spread_pct}% cheaper on {spread.cheap_venue} "
            f"({spread.cheap_ton:.2f} TON) than {spread.rich_venue} "
            f"({spread.rich_ton:.2f} TON) — {spread.spread_ton:.2f} TON gap"
        ),
        dedupe_key=f"spread:{spread.collection}:{spread.cheap_venue}:{int(spread.spread_pct)}",
        venue=spread.cheap_venue,
        price_ton=spread.cheap_ton,
        metadata={"spread_pct": spread.spread_pct, "rich_venue": spread.rich_venue},
    )


def whale_buy(sale: Sale, floor_ton: float, min_ton: float) -> Alert | None:
    """Fires on a sale that is large in absolute terms *and* well above the floor.

    Both conditions matter. Absolute size alone makes every Plush Pepe trade a whale
    alert; multiple-of-floor alone makes a 3 TON sticker sale look like conviction.
    """
    if sale.price_ton < min_ton:
        return None
    if floor_ton > 0 and sale.price_ton < floor_ton * 1.5:
        return None

    multiple = round(sale.price_ton / floor_ton, 1) if floor_ton > 0 else 0.0
    suffix = f" ({multiple}× floor)" if multiple else ""
    return Alert(
        kind="whale",
        collection=sale.collection,
        title=f"🐋 {sale.collection} {sale.price_ton:.0f} TON buy",
        body=(
            f"Whale bought {sale.collection} for {sale.price_ton:.2f} TON "
            f"on {sale.venue}{suffix}"
        ),
        dedupe_key=f"whale:{sale.venue}:{sale.gift_id}:{int(sale.price_ton)}",
        venue=sale.venue,
        price_ton=sale.price_ton,
        metadata={"floor_multiple": multiple, "onchain": sale.onchain},
    )


def watch_hit(
    collection: str,
    target_ton: float,
    current: FloorQuote,
    telegram_id: int,
) -> Alert | None:
    """Fires when a user's auto-snipe target is met. Direct message, not channel."""
    if current.floor_ton > target_ton:
        return None

    return Alert(
        kind="watch_hit",
        collection=collection,
        title=f"🎯 {collection} hit your target",
        body=(
            f"{collection} is {current.floor_ton:.2f} TON on {current.venue}, "
            f"at or below your {target_ton:.2f} TON target"
        ),
        dedupe_key=f"watch:{telegram_id}:{collection}:{round(current.floor_ton, 1)}",
        venue=current.venue,
        price_ton=current.floor_ton,
        url=current.url,
        metadata={"telegram_id": telegram_id, "target_ton": target_ton},
    )


def new_listing_burst(
    collection: str,
    previous_count: int,
    current: FloorQuote,
    min_new: int = 5,
) -> Alert | None:
    """Fires when supply floods in — often the first sign of a coordinated dump."""
    added = current.listing_count - previous_count
    if previous_count <= 0 or added < min_new:
        return None

    return Alert(
        kind="supply_burst",
        collection=collection,
        title=f"📦 {collection} +{added} new listings",
        body=(
            f"{added} new {collection} listings appeared on {current.venue} "
            f"({previous_count} → {current.listing_count}), floor {current.floor_ton:.2f} TON"
        ),
        dedupe_key=f"supply:{collection}:{current.venue}:{current.listing_count}",
        venue=current.venue,
        price_ton=current.floor_ton,
        metadata={"added": added},
    )


def format_for_telegram(alert: Alert, bot_username: str = "") -> str:
    """Render an alert as the HTML the channel actually shows."""
    lines = [f"<b>{_escape(alert.title)}</b>", "", _escape(alert.body)]
    if alert.url:
        lines.append("")
        lines.append(f'<a href="{alert.url}">Open listing →</a>')
    if bot_username:
        slug = alert.collection.replace(" ", "_")
        lines.append(f'<a href="https://t.me/{bot_username}?start=c_{slug}">Scan all venues →</a>')
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_stale(captured_at: datetime, now: datetime, max_age_seconds: int = 600) -> bool:
    """Guard against alerting on data the indexer failed to refresh."""
    return (now - captured_at).total_seconds() > max_age_seconds
