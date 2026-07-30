"""Async engine and session plumbing.

Two storage backends are supported and they need different care:

  * **SQLite** is the default and fine for a single process. It is *not* fine for
    ``giftpulse all``, where the indexer and the API write concurrently — without
    WAL and a busy timeout that combination produces ``database is locked`` under
    ordinary load.
  * **Postgres** is what a real deployment runs. It gets a bounded pool, pre-ping so
    a connection dropped by an idle timeout is discovered before a query rides it,
    and recycling well inside the usual proxy timeouts.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from giftpulse.config import get_settings
from giftpulse.models import Base

log = logging.getLogger(__name__)

# The migration scripts ship inside the package. Locating them relative to this
# module rather than to the working directory is what makes `giftpulse migrate` work
# the same from a source checkout and from an installed wheel in a container, where
# there is no alembic.ini next to the code.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    kwargs: dict = {"future": True}

    if settings.is_sqlite:
        # A 30-second busy timeout is the difference between "the other writer is
        # mid-transaction" being a pause and being an exception.
        kwargs["connect_args"] = {"timeout": 30}
        if settings.is_memory_db:
            # Every new connection to ":memory:" is a *different* database. Tests
            # rely on one shared connection for the whole engine.
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

    engine = create_async_engine(settings.database_url, **kwargs)

    if settings.is_sqlite and not settings.is_memory_db:
        _enable_sqlite_concurrency(engine)

    return engine


def _enable_sqlite_concurrency(engine: AsyncEngine) -> None:
    """Turn on WAL so readers do not block the writer.

    Applied per connection because SQLite pragmas are connection-scoped, and
    ``journal_mode=WAL`` is the one that persists to the database file itself.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def run_migrations() -> None:
    """Bring the database up to the latest Alembic revision.

    Runs in-process on startup so a single-VPS deployment cannot drift into the
    situation where the code expects a column the database has never heard of.
    Deployments with a separate release step should set
    ``GIFTPULSE_AUTO_MIGRATE=false`` and run ``giftpulse migrate`` themselves.
    """
    from alembic import command
    from alembic.config import Config

    def _upgrade(connection) -> None:  # noqa: ANN001
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_DIR))
        # Hand Alembic the live connection rather than letting it open its own —
        # env.py looks for exactly this.
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    async with get_engine().begin() as connection:
        await connection.run_sync(_upgrade)


async def init_db() -> None:
    """Prepare the schema for this process.

    Migrations are the real path. ``create_all`` remains for in-memory SQLite,
    where a migration would be applied to a throwaway database and every test would
    pay for it.
    """
    settings = get_settings()

    if settings.is_memory_db or not settings.auto_migrate:
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return

    try:
        await run_migrations()
    except Exception:
        log.exception(
            "migrations failed; falling back to create_all. The schema may be "
            "behind — run `giftpulse migrate` and check the error above."
        )
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    """Tear down the engine and reset module state (used by tests and shutdown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
