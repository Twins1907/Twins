"""Portals adapter.

Portals requires authentication. The supported path here is the short-lived
``authData`` blob captured from the Telegram Web client and passed in via
``GIFTPULSE_PORTALS_AUTH_DATA``. It expires — hours, not days — so treat 401 as a
routine event: the adapter degrades to empty results and the indexer keeps polling
the venues that still answer, rather than the whole cycle failing.

Re-auth is deliberately a human step. Automating it means holding Telegram
credentials, and this process holds no credentials it does not need.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from giftpulse.venues.base import Listing, Sale, VenueAdapter, to_ton

log = logging.getLogger(__name__)


class PortalsAdapter(VenueAdapter):
    slug = "portals"
    display_name = "Portals"
    base_url = "https://portals-market.com/api"

    def __init__(  # noqa: ANN001
        self,
        client,
        referral_code: str = "",
        auth_data: str = "",
        **pacing: object,
    ) -> None:
        super().__init__(client, referral_code, **pacing)  # type: ignore[arg-type]
        self.auth_data = auth_data
        # A missing credential is the same operational fact as a rejected one:
        # this venue is dark until a human supplies a fresh authData.
        if not auth_data:
            self.auth_expired = True

    @property
    def authenticated(self) -> bool:
        return bool(self.auth_data)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"tma {self.auth_data}",
            "Accept": "application/json",
        }

    def _warn_unauthenticated(self) -> None:
        # Once per process, not once per poll — a missing credential should not
        # bury the log at one line per collection per minute.
        if not self._auth_warned:
            log.warning(
                "portals: no authData configured, skipping. "
                "Set GIFTPULSE_PORTALS_AUTH_DATA to enable this venue."
            )
            self._auth_warned = True

    async def fetch_listings(self, collection: str, limit: int = 50) -> list[Listing]:
        if not self.authenticated:
            self._warn_unauthenticated()
            return []

        payload = await self._get_json(
            f"{self.base_url}/nfts/search",
            params={
                "filter_by_collections": collection,
                "sort_by": "price asc",
                "limit": limit,
                "status": "listed",
            },
            headers=self._headers(),
        )
        if not isinstance(payload, dict):
            return []

        results: list[Listing] = []
        for raw in payload.get("results", []) or []:
            if not isinstance(raw, dict):
                continue
            price = to_ton(raw.get("price"))
            if price is None:
                continue
            nft_id = str(raw.get("id", ""))
            results.append(
                Listing(
                    venue=self.slug,
                    collection=collection,
                    listing_id=nft_id,
                    gift_id=str(raw.get("external_collection_number", "")),
                    price_ton=price,
                    model=_attribute(raw, "model"),
                    backdrop=_attribute(raw, "backdrop"),
                    url=f"https://t.me/portals/market?startapp=gift_{nft_id}",
                )
            )
        results.sort(key=lambda item: item.price_ton)
        return results

    async def fetch_recent_sales(self, collection: str, limit: int = 50) -> list[Sale]:
        if not self.authenticated:
            self._warn_unauthenticated()
            return []

        payload = await self._get_json(
            f"{self.base_url}/market/actions/",
            params={"filter_by_collections": collection, "action_types": "buy", "limit": limit},
            headers=self._headers(),
        )
        if not isinstance(payload, dict):
            return []

        sales: list[Sale] = []
        for raw in payload.get("actions", []) or []:
            if not isinstance(raw, dict):
                continue
            price = to_ton(raw.get("amount"))
            if price is None:
                continue
            sales.append(
                Sale(
                    venue=self.slug,
                    collection=collection,
                    gift_id=str(raw.get("nft_id", "")),
                    price_ton=price,
                    buyer=str(raw.get("buyer", "") or ""),
                    seller=str(raw.get("seller", "") or ""),
                    # Portals settles buys through its own balance ledger; we do not
                    # get a chain reference here, so this stays False by design.
                    onchain=False,
                    occurred_at=_parse_ts(raw.get("created_at")),
                )
            )
        return sales


def _attribute(raw: dict, name: str) -> str:
    for attribute in raw.get("attributes", []) or []:
        if isinstance(attribute, dict) and attribute.get("type") == name:
            return str(attribute.get("value", ""))
    return ""


def _parse_ts(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
