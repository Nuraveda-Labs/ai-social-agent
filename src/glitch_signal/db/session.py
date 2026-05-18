"""Async SQLModel session factory."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from glitch_signal.config import settings


@lru_cache(maxsize=1)
def _engine() -> AsyncEngine:
    return create_async_engine(
        settings().signal_db_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def _session_factory() -> sessionmaker:
    return sessionmaker(
        bind=_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a session and commits/rolls back."""
    factory = _session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all_tables() -> None:
    """Create tables if they don't exist (dev/test only — use Alembic in prod)."""
    async with _engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
