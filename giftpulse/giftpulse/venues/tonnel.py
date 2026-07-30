"""Tonnel adapter.

Tonnel's API is the most open of the three, so this is the venue that keeps working
when the others need a credential refresh. Prices come back as plain TON floats.
"""

from __future__ import annotations

from datetime import UTC, datetime

from giftpulse.venues.base import Listing, Sale, VenueAdapter, to_ton


class TonnelAdapter(VenueAdapter):
    slug = "tonnel"
    display_name = "Tonnel"
    base_url = "https://gifts2.tonnel.network/api"

    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        payload = await self._post_json(
            f"{self.base_url}/pageGifts",
            json={
                "page": 1,
                "limit": limit,
                "sort": '{"price":1,"gift_id":-1}',
                "filter": f'{{"price":{{"$exists":true}},"buyer":{{"$exists":false}},'
                f'"gift_name":"{collection}"}}',
                "ref": 0,
                "price_range": None,
                "user_auth": "",
            },
            headers={"Accept": "application/json"},
        )
        if not isinstance(payload, list):
            return []

        results: list[Listing] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            price = to_ton(raw.get("price"))
            if price is None:
                continue
            gift_num = str(raw.get("gift_num", ""))
            results.append(
                Listing(
                    venue=self.slug,
                    collection=collection,
                    listing_id=str(raw.get("gift_id", gift_num)),
                    gift_id=gift_num,
                    price_ton=price,
                    model=str(raw.get("model", "") or ""),
                    backdrop=str(raw.get("backdrop", "") or ""),
                    url=f"https://t.me/tonnel_network_bot/gifts?startapp={gift_num}",
                    seller=str(raw.get("seller", "") or ""),
                )
            )
        results.sort(key=lambda item: item.price_ton)
        return results

    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        payload = await self._post_json(
            f"{self.base_url}/saleHistory",
            json={"page": 1, "limit": limit, "filter": f'{{"gift_name":"{collection}"}}'},
            headers={"Accept": "application/json"},
        )
        if not isinstance(payload, list):
            return []

        sales: list[Sale] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            price = to_ton(raw.get("price"))
            if price is None:
                continue
            sales.append(
                Sale(
                    venue=self.slug,
                    collection=collection,
                    gift_id=str(raw.get("gift_num", "")),
                    price_ton=price,
                    buyer=str(raw.get("buyer", "") or ""),
                    seller=str(raw.get("seller", "") or ""),
                    onchain=bool(raw.get("tx_hash")),
                    occurred_at=_parse_ts(raw.get("sold_at")),
                )
            )
        return sales


def _parse_ts(value: object) -> datetime:
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
