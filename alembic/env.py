"""Alembic environment — async SQLAlchemy.

The database URL comes from glitch_signal.config.settings (which reads
SIGNAL_DB_URL from .env). The static `sqlalchemy.url` in alembic.ini is
only used as a fallback for offline mode on a fresh clone — production
never hits it because the deployed .env is always present.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Import all models so their metadata is registered
import glitch_signal.db.models  # noqa: F401
from alembic import context
from glitch_signal.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Prefer the runtime-resolved URL from .env over the static alembic.ini value.
# This avoids shipping real credentials in alembic.ini and lets the same
# migration set target any environment (dev / staging / prod) by swapping .env.
_runtime_url = settings().signal_db_url
if _runtime_url:
    config.set_main_option("sqlalchemy.url", _runtime_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url")
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
