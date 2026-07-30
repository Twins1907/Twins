"""Alembic environment.

Supports two entry paths:

  * **In-process** — ``giftpulse.db.run_migrations`` passes a live async connection
    through ``config.attributes["connection"]``. This is what runs on startup.
  * **Command line** — ``alembic upgrade head`` with no connection supplied, in
    which case the URL comes from ``GIFTPULSE_DATABASE_URL`` like everything else.
    An async driver URL is rewritten to its sync counterpart, because the CLI path
    has no event loop to run one in.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from giftpulse.config import get_settings
from giftpulse.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """The configured database URL with any async driver swapped for a sync one."""
    url = get_settings().database_url
    return (
        url.replace("+aiosqlite", "")
        .replace("+asyncpg", "+psycopg2")
        .replace("postgresql+psycopg2", "postgresql")
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:  # noqa: ANN001
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode rewrites the table
        # instead, which keeps one migration script valid on both backends.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as new_connection:
        _run(new_connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
