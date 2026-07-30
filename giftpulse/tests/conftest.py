from __future__ import annotations

import os

import pytest

# Set before any giftpulse import so Settings picks these up rather than a
# developer's real .env.
os.environ.setdefault("GIFTPULSE_MOCK", "true")
os.environ.setdefault("GIFTPULSE_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GIFTPULSE_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")


@pytest.fixture
async def session():
    """A session against a fresh in-memory database per test."""
    from giftpulse.config import get_settings
    from giftpulse.db import dispose_db, get_engine, get_sessionmaker
    from giftpulse.models import Base

    get_settings.cache_clear()
    await dispose_db()

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with get_sessionmaker()() as db_session:
        yield db_session

    await dispose_db()


@pytest.fixture
def settings():
    from giftpulse.config import get_settings

    get_settings.cache_clear()
    return get_settings()
