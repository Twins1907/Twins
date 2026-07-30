"""Process entry points.

Four long-running processes, deliberately separate so they can be scaled and
restarted independently:

  giftpulse indexer   poll venues, write snapshots, fire alerts
  giftpulse bot       Telegram command surface
  giftpulse api       Mini App backend + static shell
  giftpulse all       all three in one process (fine for a single VPS)

plus one that exits:

  giftpulse migrate   bring the database schema to head
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
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


def _install_shutdown_handler() -> asyncio.Event:
    """Resolve an Event on SIGTERM/SIGINT.

    Container runtimes send SIGTERM and then wait a fixed grace period before
    SIGKILL. Without this the default handler tears the loop down mid-cycle, which
    at worst drops a partially written batch of snapshots.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_number, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows, and any non-main thread. The KeyboardInterrupt path still
            # covers the interactive case.
            pass
    return stop


async def _run_until_signal(task: asyncio.Task, stop: asyncio.Event) -> int:
    """Wait for the task, or for a shutdown signal, whichever lands first."""
    waiter = asyncio.create_task(stop.wait(), name="shutdown")
    done, _pending = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)

    if waiter in done and not task.done():
        logging.getLogger(__name__).info("shutdown signal received, stopping cleanly")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        waiter.cancel()
        return 0

    waiter.cancel()
    exception = task.exception() if task.done() and not task.cancelled() else None
    if exception is not None:
        logging.getLogger(__name__).error("exited: %s", exception)
        return 1
    return 0


async def _run_migrations() -> int:
    from giftpulse.db import dispose_db, run_migrations

    settings = get_settings()
    if settings.is_memory_db:
        print("Database is in-memory; nothing to migrate.")
        return 0
    try:
        await run_migrations()
        print(f"Schema is at head: {settings.database_url}")
        return 0
    finally:
        await dispose_db()


async def _run_indexer(once: bool, dry_run: bool, share_pool: bool = False) -> int:
    from giftpulse.alerts.dispatcher import AlertDispatcher
    from giftpulse.db import init_db
    from giftpulse.indexer.service import IndexerService
    from giftpulse.venues.registry import get_shared_pool

    settings = get_settings()
    await init_db()

    bot = None
    if not dry_run and settings.bot_token and (settings.alert_channel or settings.admin_chat_id):
        from giftpulse.bot.app import build_bot

        bot = build_bot(settings)

    dispatcher = AlertDispatcher(bot=bot, settings=settings)
    if bot is not None:
        me = await bot.get_me()
        dispatcher.bot_username = me.username or ""

    # Under `all` the bot and API are in this process too, and each pool carries its
    # own rate limiter — three pools would mean three times the configured
    # per-venue ceiling.
    service = IndexerService(
        settings=settings,
        dispatcher=dispatcher,
        pool=get_shared_pool(settings) if share_pool else None,
    )
    try:
        if once:
            for warning in settings.startup_warnings():
                print(f"warning: {warning}")
            report = await service.run_cycle()
            print(f"Cycle complete: {report.summary()}")
            return 0
        await service.run_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopped.")
    finally:
        await service.aclose()
        if bot is not None:
            await bot.session.close()
    return 0


async def _run_bot() -> int:
    from giftpulse.bot.app import run_bot

    try:
        await run_bot()
    except (KeyboardInterrupt, asyncio.CancelledError):
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
    log = logging.getLogger(__name__)

    for warning in settings.startup_warnings():
        log.warning("startup: %s", warning)

    if settings.is_sqlite and not settings.is_memory_db:
        # WAL makes this survivable rather than advisable. Two writers on one
        # SQLite file is a scaling ceiling you will meet without being told.
        log.warning(
            "running `all` on SQLite: the indexer and API write concurrently. "
            "WAL is enabled, but move to Postgres before real traffic "
            "(GIFTPULSE_DATABASE_URL=postgresql+asyncpg://...)."
        )

    config = uvicorn.Config(
        "giftpulse.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    # uvicorn installs its own handlers otherwise, and they race ours.
    server.install_signal_handlers = lambda: None

    stop = _install_shutdown_handler()
    tasks = [
        asyncio.create_task(
            _run_indexer(once=False, dry_run=False, share_pool=True), name="indexer"
        ),
        asyncio.create_task(server.serve(), name="api"),
    ]
    if settings.bot_token:
        tasks.append(asyncio.create_task(run_bot(), name="bot"))
    else:
        log.warning("GIFTPULSE_BOT_TOKEN not set — running indexer and API only.")

    supervisor = asyncio.create_task(
        asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION), name="supervisor"
    )
    waiter = asyncio.create_task(stop.wait(), name="shutdown")
    await asyncio.wait({supervisor, waiter}, return_when=asyncio.FIRST_COMPLETED)

    if stop.is_set():
        log.info("shutdown signal received, stopping cleanly")
    server.should_exit = True

    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    supervisor.cancel()
    waiter.cancel()

    exit_code = 0
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, Exception):
            log.error("%s exited: %s", task.get_name(), result)
            exit_code = 1
    return 0 if stop.is_set() else exit_code


async def _indexer_entrypoint(once: bool, dry_run: bool) -> int:
    if once:
        return await _run_indexer(once=True, dry_run=dry_run)
    stop = _install_shutdown_handler()
    task = asyncio.create_task(_run_indexer(once=False, dry_run=dry_run), name="indexer")
    return await _run_until_signal(task, stop)


async def _bot_entrypoint() -> int:
    stop = _install_shutdown_handler()
    task = asyncio.create_task(_run_bot(), name="bot")
    return await _run_until_signal(task, stop)


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
    sub.add_parser("migrate", help="bring the database schema to head and exit")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "indexer":
        return asyncio.run(_indexer_entrypoint(once=args.once, dry_run=args.dry_run))
    if args.command == "bot":
        return asyncio.run(_bot_entrypoint())
    if args.command == "api":
        return _run_api()
    if args.command == "all":
        return asyncio.run(_run_all())
    if args.command == "migrate":
        return asyncio.run(_run_migrations())
    return 1


if __name__ == "__main__":
    sys.exit(cli())
