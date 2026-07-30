"""Process entry points.

Three long-running processes, deliberately separate so they can be scaled and
restarted independently:

  giftpulse indexer   poll venues, write snapshots, fire alerts
  giftpulse bot       Telegram command surface
  giftpulse api       Mini App backend + static shell
  giftpulse all       all three in one process (fine for a single VPS)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from giftpulse.config import get_settings


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _run_indexer(once: bool, dry_run: bool) -> int:
    from giftpulse.alerts.dispatcher import AlertDispatcher
    from giftpulse.db import init_db
    from giftpulse.indexer.service import IndexerService

    settings = get_settings()
    await init_db()

    bot = None
    if not dry_run and settings.bot_token and settings.alert_channel:
        from giftpulse.bot.app import build_bot

        bot = build_bot(settings)

    dispatcher = AlertDispatcher(bot=bot, settings=settings)
    if bot is not None:
        me = await bot.get_me()
        dispatcher.bot_username = me.username or ""

    service = IndexerService(settings=settings, dispatcher=dispatcher)
    try:
        if once:
            report = await service.run_cycle()
            print(f"Cycle complete: {report.summary()}")
            return 0
        await service.run_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if bot is not None:
            await bot.session.close()
    return 0


async def _run_bot() -> int:
    from giftpulse.bot.app import run_bot

    try:
        await run_bot()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def _run_api() -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "giftpulse.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    return 0


async def _run_all() -> int:
    """Indexer, bot, and API together. Convenient for a single VPS.

    If any one task dies the whole process exits rather than limping along with a
    silently dead indexer — a supervisor restarting everything is far easier to
    reason about than a half-running system.
    """
    import uvicorn

    from giftpulse.bot.app import run_bot

    settings = get_settings()
    config = uvicorn.Config(
        "giftpulse.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    tasks = [
        asyncio.create_task(_run_indexer(once=False, dry_run=False), name="indexer"),
        asyncio.create_task(server.serve(), name="api"),
    ]
    if settings.bot_token:
        tasks.append(asyncio.create_task(run_bot(), name="bot"))
    else:
        logging.getLogger(__name__).warning(
            "GIFTPULSE_BOT_TOKEN not set — running indexer and API only."
        )

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exception = task.exception()
        if exception is not None:
            logging.getLogger(__name__).error("%s exited: %s", task.get_name(), exception)
            return 1
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="giftpulse", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    indexer = sub.add_parser("indexer", help="poll venues and fire alerts")
    indexer.add_argument("--once", action="store_true", help="run a single cycle and exit")
    indexer.add_argument(
        "--dry-run", action="store_true", help="evaluate alerts but do not send them"
    )

    sub.add_parser("bot", help="run the Telegram bot")
    sub.add_parser("api", help="run the Mini App backend")
    sub.add_parser("all", help="run indexer, bot, and API together")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "indexer":
        return asyncio.run(_run_indexer(once=args.once, dry_run=args.dry_run))
    if args.command == "bot":
        return asyncio.run(_run_bot())
    if args.command == "api":
        return _run_api()
    if args.command == "all":
        return asyncio.run(_run_all())
    return 1


if __name__ == "__main__":
    sys.exit(cli())
