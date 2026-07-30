"""MRKT adapter.

MRKT quotes in nanoton integers rather than TON floats, which is exactly the kind of
per-venue detail the adapter layer exists to absorb.
"""

from __future__ import annotations

from datetime import UTC, datetime

from giftpulse.venues.base import Listing, Sale, VenueAdapter, to_ton

NANOTON = 1_000_000_000.0


class MrktAdapter(VenueAdapter):
    slug = "mrkt"
    display_name = "MRKT"
    base_url = "https://api.tgmrkt.io/api/v1"

    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        payload = await self._get_json(
            f"{self.base_url}/gifts",
            params={"collection": collection, "sort": "price_asc", "count": limit},
            headers={"Accept": "application/json"},
        )
        rows = _rows(payload, "gifts")

        results: list[Listing] = []
        for raw in rows:
            price = to_ton(raw.get("price"), divisor=NANOTON)
            if price is None:
                continue
            gift_id = str(raw.get("id", ""))
            results.append(
                Listing(
                    venue=self.slug,
                    collection=collection,
                    listing_id=gift_id,
                    gift_id=str(raw.get("number", "")),
                    price_ton=price,
                    model=str(raw.get("model", "") or ""),
                    backdrop=str(raw.get("backdrop", "") or ""),
                    url=f"https://t.me/mrkt/app?startapp={gift_id}",
                    seller=str(raw.get("owner", "") or ""),
                )
            )
        results.sort(key=lambda item: item.price_ton)
        return results

    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        payload = await self._get_json(
            f"{self.base_url}/activities",
            params={"collection": collection, "type": "sale", "count": limit},
            headers={"Accept": "application/json"},
        )
        rows = _rows(payload, "activities")

        sales: list[Sale] = []
        for raw in rows:
            price = to_ton(raw.get("price"), divisor=NANOTON)
            if price is None:
                continue
            sales.append(
                Sale(
                    venue=self.slug,
                    collection=collection,
                    gift_id=str(raw.get("giftId", raw.get("id", ""))),
                    price_ton=price,
                    buyer=str(raw.get("to", "") or ""),
                    seller=str(raw.get("from", "") or ""),
                    onchain=bool(raw.get("txHash")),
                    occurred_at=_parse_ts(raw.get("createdAt")),
                )
            )
        return sales


def _rows(payload: object, key: str) -> list[dict]:
    """MRKT returns a bare list on some endpoints and a wrapped object on others."""
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = payload.get(key) or payload.get("items") or []
    else:
        return []
    return [row for row in candidates if isinstance(row, dict)]


def _parse_ts(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(UTC)
