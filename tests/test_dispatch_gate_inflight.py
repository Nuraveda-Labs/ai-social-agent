"""Dispatch gates count in-flight posts, not just finalised ones.

Regression test for the 2026-04-18 Namhya over-fire: 11 posts fired
inside a single 15-min slot when the daily_cap was 3, because the
gate counted `PublishedPost` only and Upload-Post's webhook-async
flow doesn't write that row until ~10 minutes after dispatch.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("DISPATCH_MODE", "dry_run")
os.environ.setdefault("SIGNAL_DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_SIGNAL", "0:test")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_API_KEY", "test")
os.environ.setdefault("AUTH_ENCRYPTION_KEY", "l3mgT3MDKZ2g8oh2l8r4e1XaS0o7Q8mT9H5V1v3P2Hk=")


@pytest.fixture(autouse=True)
def _reset_caches():
    from glitch_signal import config as cfg
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    yield


async def _build_test_db():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    import glitch_signal.db.session as db_session
    import glitch_signal.scheduler.queue as q

    def _getter():
        return factory
    originals = {}
    for mod in (db_session, q):
        if hasattr(mod, "_session_factory"):
            originals[mod] = mod._session_factory
            mod._session_factory = _getter
    return factory, originals


def _restore(originals):
    for mod, orig in originals.items():
        mod._session_factory = orig


async def _seed_sp(
    factory, *, brand_id: str, status: str,
    last_attempt_at: datetime, asset_required: bool = True,
):
    from glitch_signal.db.models import ScheduledPost, VideoAsset

    asset_id = str(uuid.uuid4())
    sp_id = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)

    async with factory() as session:
        if asset_required:
            session.add(VideoAsset(
                id=asset_id, script_id=str(uuid.uuid4()),
                file_path="/tmp/x.mp4", duration_s=1.0, created_at=now,
            ))
        session.add(ScheduledPost(
            id=sp_id, brand_id=brand_id, asset_id=asset_id,
            platform="upload_post_tiktok", scheduled_for=now,
            status=status, last_attempt_at=last_attempt_at,
        ))
        await session.commit()
    return sp_id


class TestCountPostsTodayIncludesInflight:
    @pytest.mark.asyncio
    async def test_awaiting_webhook_counts_against_cap(self):
        """A ScheduledPost still waiting for its webhook callback MUST
        be counted — otherwise every 30s tick fires another post."""
        from glitch_signal.scheduler.queue import _count_posts_today

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            # Two posts dispatched recently, neither finalised yet.
            await _seed_sp(factory, brand_id="b", status="awaiting_webhook",
                           last_attempt_at=now - timedelta(minutes=2))
            await _seed_sp(factory, brand_id="b", status="dispatching",
                           last_attempt_at=now - timedelta(seconds=30))

            count = await _count_posts_today("b", now)
            assert count == 2
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_done_posts_count_too(self):
        from glitch_signal.scheduler.queue import _count_posts_today

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            await _seed_sp(factory, brand_id="b", status="done",
                           last_attempt_at=now - timedelta(hours=1))
            await _seed_sp(factory, brand_id="b", status="awaiting_webhook",
                           last_attempt_at=now - timedelta(minutes=2))

            count = await _count_posts_today("b", now)
            assert count == 2
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_queued_not_counted(self):
        """Posts that haven't been dispatched yet (still `queued` /
        `pending_veto`) must NOT count against today's cap."""
        from glitch_signal.scheduler.queue import _count_posts_today

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            await _seed_sp(factory, brand_id="b", status="queued",
                           last_attempt_at=None)
            await _seed_sp(factory, brand_id="b", status="pending_veto",
                           last_attempt_at=None)

            count = await _count_posts_today("b", now)
            assert count == 0
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_yesterdays_posts_not_counted(self):
        from glitch_signal.scheduler.queue import _count_posts_today

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            await _seed_sp(factory, brand_id="b", status="done",
                           last_attempt_at=now - timedelta(days=1, hours=5))
            await _seed_sp(factory, brand_id="b", status="awaiting_webhook",
                           last_attempt_at=now - timedelta(minutes=2))

            count = await _count_posts_today("b", now)
            assert count == 1   # only today's
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_other_brands_not_counted(self):
        from glitch_signal.scheduler.queue import _count_posts_today

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            await _seed_sp(factory, brand_id="b", status="done",
                           last_attempt_at=now - timedelta(hours=1))
            await _seed_sp(factory, brand_id="other", status="done",
                           last_attempt_at=now - timedelta(hours=1))

            assert await _count_posts_today("b", now) == 1
            assert await _count_posts_today("other", now) == 1
        finally:
            _restore(originals)


class TestMinIntervalUsesInflightTimestamp:
    @pytest.mark.asyncio
    async def test_recent_dispatch_blocks_even_before_webhook(self):
        """min_interval must read ScheduledPost.last_attempt_at, not
        PublishedPost.published_at. Otherwise a still-in-flight post
        registers as 'never' and the gate lets the next dispatch through."""
        from glitch_signal.scheduler.queue import _minutes_since_last_post

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            await _seed_sp(factory, brand_id="b", status="awaiting_webhook",
                           last_attempt_at=now - timedelta(minutes=5))
            # No PublishedPost row exists — the old bug would have this
            # return None.
            mins = await _minutes_since_last_post("b", now)
            assert mins is not None
            assert 4.9 <= mins <= 5.1
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_history(self):
        from glitch_signal.scheduler.queue import _minutes_since_last_post

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            mins = await _minutes_since_last_post("b", now)
            assert mins is None
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_queued_ignored(self):
        """A queued post hasn't been dispatched yet; it shouldn't count
        as 'most recent post'."""
        from glitch_signal.scheduler.queue import _minutes_since_last_post

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            await _seed_sp(factory, brand_id="b", status="queued",
                           last_attempt_at=None)
            mins = await _minutes_since_last_post("b", now)
            assert mins is None
        finally:
            _restore(originals)
