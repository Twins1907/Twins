"""FastAPI backend for the Mini App.

Read endpoints are public — floors are public market data and gating them just adds
latency. Anything tied to a user (watches, quotes attributed to an account) requires
a verified ``initData`` header.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from giftpulse.analytics import (
    change_pct,
    collection_overview,
    floor_history,
    latest_floors,
    onchain_volume_ton,
    routed_volume_ton,
)
from giftpulse.api.auth import InitDataError, TelegramUser, verify_init_data
from giftpulse.config import get_settings
from giftpulse.db import init_db, session_scope
from giftpulse.indexer.service import TRACKED_COLLECTIONS
from giftpulse.models import RoutedTrade, User, Watch
from giftpulse.routing.engine import RoutingEngine
from giftpulse.venues.registry import build_adapters, build_client

log = logging.getLogger(__name__)

MINIAPP_DIR = Path(__file__).resolve().parent.parent / "miniapp"


async def current_user(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> TelegramUser:
    settings = get_settings()
    try:
        return verify_init_data(x_telegram_init_data, settings.bot_token)
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


class WatchRequest(BaseModel):
    collection: str = Field(min_length=1, max_length=128)
    target_ton: float = Field(gt=0)


class QuoteRequest(BaseModel):
    collection: str = Field(min_length=1, max_length=128)
    max_price_ton: float | None = Field(default=None, gt=0)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="GiftPulse", version="0.1.0", lifespan=lifespan)

    # --- public market data ------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        settings = get_settings()
        return {
            "status": "ok",
            "mock": settings.mock,
            "venues": settings.enabled_venues,
            "fee_bps": settings.execution_fee_bps,
        }

    @app.get("/api/collections")
    async def collections() -> list[dict]:
        async with session_scope() as session:
            return await collection_overview(session, TRACKED_COLLECTIONS)

    @app.get("/api/floors/{collection}")
    async def floors(collection: str) -> dict:
        async with session_scope() as session:
            venues = await latest_floors(session, collection)
            delta = await change_pct(session, collection, hours=24)
            volume = await onchain_volume_ton(session, collection, days=7)
        if not venues:
            raise HTTPException(status_code=404, detail="no indexed data for this collection")
        return {
            "collection": collection,
            "venues": venues,
            "change_24h_pct": delta,
            "onchain_volume_7d_ton": volume,
        }

    @app.get("/api/history/{collection}")
    async def history(collection: str, hours: int = Query(default=24, ge=1, le=720)) -> list[dict]:
        async with session_scope() as session:
            return await floor_history(session, collection, hours=hours)

    @app.get("/api/stats")
    async def stats() -> dict:
        async with session_scope() as session:
            return await routed_volume_ton(session, days=30)

    # --- authenticated -----------------------------------------------------

    @app.post("/api/quote")
    async def quote(request: QuoteRequest, user: TelegramUser = Depends(current_user)) -> dict:
        """Best executable price across venues, recorded against the user."""
        settings = get_settings()
        client = build_client(settings)
        try:
            engine = RoutingEngine(build_adapters(client, settings), settings)
            best = await engine.best_buy(request.collection, request.max_price_ton)
        finally:
            await client.aclose()

        if best is None:
            raise HTTPException(status_code=404, detail="no listings match that request")

        async with session_scope() as session:
            session.add(
                RoutedTrade(
                    telegram_id=user.telegram_id,
                    venue=best.venue,
                    collection=best.listing.collection,
                    listing_id=best.listing.listing_id,
                    price_ton=best.price_ton,
                    fee_ton=best.fee_ton,
                    status="built",
                )
            )

        return {
            "venue": best.venue,
            "collection": best.listing.collection,
            "listing_id": best.listing.listing_id,
            "price_ton": best.price_ton,
            "fee_ton": best.fee_ton,
            "total_ton": best.total_ton,
            "deep_link": best.deep_link,
            "runner_up_venue": best.runner_up_venue,
            "runner_up_price_ton": best.runner_up_price_ton,
            "savings_ton": best.savings_ton,
            "savings_pct": best.savings_pct,
            "model": best.listing.model,
            "backdrop": best.listing.backdrop,
        }

    @app.get("/api/watches")
    async def list_watches(user: TelegramUser = Depends(current_user)) -> list[dict]:
        async with session_scope() as session:
            result = await session.execute(
                select(Watch).where(
                    Watch.telegram_id == user.telegram_id, Watch.active.is_(True)
                )
            )
            return [
                {"collection": watch.collection, "target_ton": watch.target_ton}
                for watch in result.scalars()
            ]

    @app.post("/api/watches")
    async def create_watch(
        request: WatchRequest, user: TelegramUser = Depends(current_user)
    ) -> dict:
        async with session_scope() as session:
            result = await session.execute(
                select(Watch).where(
                    Watch.telegram_id == user.telegram_id,
                    Watch.collection == request.collection,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.target_ton = request.target_ton
                existing.active = True
            else:
                session.add(
                    Watch(
                        telegram_id=user.telegram_id,
                        collection=request.collection,
                        target_ton=request.target_ton,
                    )
                )
        return {"ok": True, "collection": request.collection, "target_ton": request.target_ton}

    @app.post("/api/wallet")
    async def link_wallet(
        payload: dict, user: TelegramUser = Depends(current_user)
    ) -> dict:
        """Store the user's public TON address after a TON Connect handshake.

        Public address only. There is no endpoint in this application that accepts
        a private key or seed phrase, and there must never be one.
        """
        address = str(payload.get("address", "")).strip()
        if not address or len(address) > 96:
            raise HTTPException(status_code=400, detail="a valid TON address is required")

        async with session_scope() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == user.telegram_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                session.add(
                    User(
                        telegram_id=user.telegram_id,
                        username=user.username,
                        wallet_address=address,
                    )
                )
            else:
                record.wallet_address = address
        return {"ok": True, "address": address}

    # --- Mini App shell ----------------------------------------------------

    @app.get("/", response_model=None)
    async def index():  # noqa: ANN202 - returns one of two Response types
        page = MINIAPP_DIR / "index.html"
        if not page.exists():
            return JSONResponse({"detail": "Mini App bundle not found"}, status_code=404)
        return FileResponse(page)

    @app.get("/tonconnect-manifest.json")
    async def manifest() -> dict:
        settings = get_settings()
        return {
            "url": settings.public_url,
            "name": "GiftPulse",
            "iconUrl": f"{settings.public_url}/icon.png",
        }

    return app


app = create_app()
