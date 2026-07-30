"""Bot command and callback handlers."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from giftpulse.analytics import change_pct, latest_floors
from giftpulse.bot.keyboards import buy_menu, collection_menu, main_menu
from giftpulse.config import get_settings
from giftpulse.db import session_scope
from giftpulse.indexer.service import TRACKED_COLLECTIONS
from giftpulse.models import User, Watch
from giftpulse.routing.engine import RoutingEngine
from giftpulse.routing.referral import parse_start_payload, user_referral_code
from giftpulse.venues.registry import get_shared_pool

log = logging.getLogger(__name__)
router = Router()

WELCOME = (
    "<b>GiftPulse</b> — cross-venue execution for Telegram Gifts\n\n"
    "I scan Portals, Tonnel and MRKT together, tell you which venue is cheapest "
    "right now, and hand you a one-tap buy link.\n\n"
    "<b>Your keys stay yours.</b> GiftPulse never holds funds, gifts, or private "
    "keys — every purchase is signed in your own wallet.\n\n"
    "/scan — cheapest venue per collection\n"
    "/floor &lt;collection&gt; — full venue breakdown\n"
    "/watch &lt;collection&gt; &lt;price&gt; — alert me at a target\n"
    "/portfolio — your watches and stats"
)


@router.message(CommandStart())
async def start(message: Message) -> None:
    payload = parse_start_payload(_start_payload(message.text or ""))

    async with session_scope() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            session.add(
                User(
                    telegram_id=message.from_user.id,
                    username=message.from_user.username or "",
                    referred_by=payload.get("referrer", ""),
                )
            )

    # Alert deep links carry the collection, so a tap goes straight to the price.
    if "collection" in payload:
        await _send_floor(message, payload["collection"])
        return

    await message.answer(WELCOME, parse_mode="HTML", reply_markup=main_menu())


@router.message(Command("scan"))
async def scan(message: Message) -> None:
    await message.answer(
        "Pick a collection to scan across every venue:",
        reply_markup=collection_menu(TRACKED_COLLECTIONS),
    )


@router.message(Command("floor"))
async def floor_command(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/floor Plush Pepe</code>",
            parse_mode="HTML",
            reply_markup=collection_menu(TRACKED_COLLECTIONS),
        )
        return
    await _send_floor(message, parts[1].strip())


@router.message(Command("watch"))
async def watch_command(message: Message) -> None:
    parts = (message.text or "").rsplit(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/watch Plush Pepe 3800</code>\n"
            "I'll message you when the floor hits your price on any venue.",
            parse_mode="HTML",
        )
        return

    collection = parts[0].replace("/watch", "", 1).strip()
    try:
        target = float(parts[1])
    except ValueError:
        await message.answer(
            "That target price isn't a number. Try: <code>/watch Toy Bear 40</code>",
            parse_mode="HTML",
        )
        return

    if target <= 0:
        await message.answer("Target price must be above zero.")
        return

    async with session_scope() as session:
        result = await session.execute(
            select(Watch).where(
                Watch.telegram_id == message.from_user.id, Watch.collection == collection
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.target_ton = target
            existing.active = True
        else:
            session.add(
                Watch(
                    telegram_id=message.from_user.id, collection=collection, target_ton=target
                )
            )

    await message.answer(
        f"🎯 Watching <b>{collection}</b> at <b>{target:.2f} TON</b>.\n"
        "I'll message you the moment any venue's floor reaches it.",
        parse_mode="HTML",
    )


@router.message(Command("portfolio"))
async def portfolio(message: Message) -> None:
    async with session_scope() as session:
        result = await session.execute(
            select(Watch).where(
                Watch.telegram_id == message.from_user.id, Watch.active.is_(True)
            )
        )
        watches = list(result.scalars())

    if not watches:
        await message.answer(
            "No active watches yet.\n\nSet one with <code>/watch Plush Pepe 3800</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    lines = ["<b>Your watches</b>", ""]
    lines += [f"🎯 {watch.collection} — {watch.target_ton:.2f} TON" for watch in watches]
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_menu())


@router.callback_query(F.data == "menu")
async def menu_callback(query: CallbackQuery) -> None:
    await query.message.edit_text(WELCOME, parse_mode="HTML", reply_markup=main_menu())
    await query.answer()


@router.callback_query(F.data == "scan")
async def scan_callback(query: CallbackQuery) -> None:
    await query.message.edit_text(
        "Pick a collection to scan across every venue:",
        reply_markup=collection_menu(TRACKED_COLLECTIONS),
    )
    await query.answer()


@router.callback_query(F.data == "referral")
async def referral_callback(query: CallbackQuery) -> None:
    me = await query.bot.get_me()
    code = user_referral_code(query.from_user.id)
    await query.message.edit_text(
        "<b>Invite & earn</b>\n\n"
        "Share your link. When someone you invite trades through GiftPulse, "
        "you earn a share of the routing revenue.\n\n"
        f"<code>https://t.me/{me.username}?start=ref_{code}</code>",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
    await query.answer()


@router.callback_query(F.data.startswith("floor:"))
async def floor_callback(query: CallbackQuery) -> None:
    collection = query.data.split(":", 1)[1]
    text, markup = await _floor_text(collection)
    await query.message.edit_text(
        text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
    )
    await query.answer()


@router.callback_query(F.data.startswith("watch:"))
async def watch_callback(query: CallbackQuery) -> None:
    collection = query.data.split(":", 1)[1]
    await query.answer(
        f"Set a target with /watch {collection} <price>", show_alert=True
    )


@router.callback_query(F.data == "spreads")
async def spreads_callback(query: CallbackQuery) -> None:
    from giftpulse.analytics import collection_overview

    async with session_scope() as session:
        overview = await collection_overview(session, TRACKED_COLLECTIONS)

    if not overview:
        await query.answer("No data indexed yet — give the indexer a minute.", show_alert=True)
        return

    lines = ["<b>Widest cross-venue spreads</b>", ""]
    for item in overview[:8]:
        lines.append(
            f"{item['collection']} — <b>{item['spread_pct']}%</b> "
            f"(cheapest {item['best_venue']} @ {item['best_floor_ton']:.2f} TON)"
        )
    await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=main_menu())
    await query.answer()


@router.callback_query(F.data == "watches")
async def watches_callback(query: CallbackQuery) -> None:
    async with session_scope() as session:
        result = await session.execute(
            select(Watch).where(
                Watch.telegram_id == query.from_user.id, Watch.active.is_(True)
            )
        )
        watches = list(result.scalars())

    if not watches:
        await query.answer("No watches yet. Use /watch <collection> <price>", show_alert=True)
        return

    lines = ["<b>Your watches</b>", ""]
    lines += [f"🎯 {watch.collection} — {watch.target_ton:.2f} TON" for watch in watches]
    await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=main_menu())
    await query.answer()


# --- shared rendering -----------------------------------------------------


async def _floor_text(collection: str):  # noqa: ANN202
    settings = get_settings()
    pool = get_shared_pool(settings)
    router_engine = RoutingEngine(pool.adapters, settings)
    quote = await router_engine.best_buy(collection)

    if quote is None:
        # Fall back to indexed data — a live venue hiccup should not leave the
        # user staring at nothing when we have a recent snapshot on disk.
        async with session_scope() as session:
            floors = await latest_floors(session, collection)
        if not floors:
            return (
                f"No listings found for <b>{collection}</b> right now.",
                collection_menu(TRACKED_COLLECTIONS),
            )
        lines = [f"<b>{collection}</b> (last indexed)", ""]
        lines += [f"{row['venue']} — {row['floor_ton']:.2f} TON" for row in floors]
        return "\n".join(lines), collection_menu(TRACKED_COLLECTIONS)

    async with session_scope() as session:
        delta = await change_pct(session, collection, hours=24)

    lines = [
        f"<b>{collection}</b>",
        "",
        f"💎 Cheapest: <b>{quote.price_ton:.2f} TON</b> on <b>{quote.venue}</b>",
    ]
    if quote.runner_up_venue:
        lines.append(
            f"↔️ Next best: {quote.runner_up_price_ton:.2f} TON on {quote.runner_up_venue} "
            f"— you save {quote.savings_ton:.2f} TON ({quote.savings_pct}%)"
        )
    if delta is not None:
        arrow = "▼" if delta < 0 else "▲"
        lines.append(f"{arrow} 24h floor change: {delta:+.2f}%")
    if quote.fee_ton > 0:
        lines.append(f"⚙️ Routing fee: {quote.fee_ton:.3f} TON (total {quote.total_ton:.2f} TON)")
    if quote.listing.model:
        lines.append(f"🎨 {quote.listing.model} / {quote.listing.backdrop}")

    lines += ["", "<i>You sign in your own wallet. GiftPulse never holds your keys.</i>"]
    return "\n".join(lines), buy_menu(collection, quote.deep_link)


async def _send_floor(message: Message, collection: str) -> None:
    text, markup = await _floor_text(collection)
    await message.answer(
        text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
    )


def _start_payload(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
