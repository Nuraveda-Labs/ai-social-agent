"""Smoke tests for Social Signal.

All tests use DISPATCH_MODE=dry_run and mock external API calls.
These run without any network access or real credentials.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC

import pytest

# Force dry-run for all tests
os.environ["DISPATCH_MODE"] = "dry_run"
os.environ["SIGNAL_DB_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["TELEGRAM_BOT_TOKEN_SIGNAL"] = "0:test"
os.environ["TELEGRAM_ADMIN_IDS"] = "0"
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ["GOOGLE_API_KEY"] = "test"


# ---------------------------------------------------------------------------
# 1. Guardrail tests — hard stops must fire, never create OrmResponse
# ---------------------------------------------------------------------------

class TestGuardrails:
    """Pure rule engine — no network, no DB."""

    def test_hard_stop_legal(self):
        from social_signal.orm.guardrails import check
        is_safe, phrase = check("You're breaking SEC regulations!")
        assert not is_safe
        assert "SEC" in phrase

    def test_hard_stop_loss(self):
        from social_signal.orm.guardrails import check
        is_safe, phrase = check("I lost $500 trading with your bot")
        assert not is_safe
        assert phrase in ("loss", "lost $")

    def test_hard_stop_guarantee(self):
        from social_signal.orm.guardrails import check
        is_safe, _ = check("Can you guarantee returns of 20%?")
        assert not is_safe

    def test_hard_stop_lawsuit(self):
        from social_signal.orm.guardrails import check
        is_safe, phrase = check("I'm going to take legal action against you")
        assert not is_safe

    def test_safe_positive(self):
        from social_signal.orm.guardrails import check
        is_safe, phrase = check("Great bot! The cobra mascot is amazing.")
        assert is_safe
        assert phrase is None

    def test_safe_faq(self):
        from social_signal.orm.guardrails import check
        is_safe, phrase = check("How do I get access to the trading platform?")
        assert is_safe
        assert phrase is None

    def test_case_insensitive(self):
        from social_signal.orm.guardrails import check
        # "SEBI" in uppercase
        is_safe, _ = check("This violates sebi rules")
        assert not is_safe


# ---------------------------------------------------------------------------
# 2. VideoRouter — deterministic, no LLM, no DB
# ---------------------------------------------------------------------------

class TestVideoRouter:
    """Routing table maps hints to models per brand.config."""

    @pytest.mark.asyncio
    async def test_dry_run_forces_mock(self):
        from social_signal.agent.nodes.video_router import video_router_node

        state = {
            "shots": [
                {"visual": "hero shot", "duration_s": 5, "model_hint": "cinematic"},
                {"visual": "product demo", "duration_s": 5, "model_hint": "realistic"},
            ]
        }
        result = await video_router_node(state)
        routed = result["routed_shots"]
        assert len(routed) == 2
        # dry_run forces all to "mock"
        assert all(s["model"] == "mock" for s in routed)

    @pytest.mark.asyncio
    async def test_empty_shots_returns_error(self):
        from social_signal.agent.nodes.video_router import video_router_node

        state = {"shots": []}
        result = await video_router_node(state)
        assert "error" in result


# ---------------------------------------------------------------------------
# 3. Kling mock — dry_run returns mock result
# ---------------------------------------------------------------------------

class TestKlingMock:
    @pytest.mark.asyncio
    async def test_generate_dry_run(self):
        from social_signal.video_models.base import VideoGenerationRequest
        from social_signal.video_models.kling import KlingModel

        model = KlingModel()
        req = VideoGenerationRequest(prompt="cobra in neon city", duration_s=5)
        result = await model.generate(req)

        assert result.api_job_id.startswith("mock-")
        assert result.status == "pending"
        assert result.cost_usd == pytest.approx(0.14, abs=0.01)

    @pytest.mark.asyncio
    async def test_poll_dry_run(self):
        from social_signal.video_models.kling import KlingModel

        model = KlingModel()
        result = await model.poll("mock-abc123")

        assert result.status == "done"
        assert result.video_url is not None

    def test_get_model_unknown_raises(self):
        from social_signal.video_models.kling import get_model
        with pytest.raises(ValueError, match="Unknown video model"):
            get_model("nonexistent_model")


# ---------------------------------------------------------------------------
# 4. Scheduler veto window promotion — in-memory SQLite
# ---------------------------------------------------------------------------

class TestSchedulerVetoPromotion:
    @pytest.mark.asyncio
    async def test_veto_deadline_promotes_to_queued(self):
        from datetime import datetime, timedelta

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.orm import sessionmaker
        from sqlmodel import SQLModel
        from sqlmodel.ext.asyncio.session import AsyncSession

        # In-memory DB for this test
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        # Create an expired pending_veto post
        from social_signal.db.models import ScheduledPost, VideoAsset

        now = datetime.now(UTC).replace(tzinfo=None)
        past = now - timedelta(seconds=1)

        asset_id = str(uuid.uuid4())
        sp_id = str(uuid.uuid4())

        async with factory() as session:
            # Need a VideoAsset first (FK constraint)
            asset = VideoAsset(
                id=asset_id,
                script_id=str(uuid.uuid4()),
                file_path="/tmp/test.mp4",
                duration_s=30.0,
                created_at=now,
            )
            session.add(asset)
            sp = ScheduledPost(
                id=sp_id,
                asset_id=asset_id,
                platform="youtube_shorts",
                scheduled_for=now,
                status="pending_veto",
                veto_deadline=past,
            )
            session.add(sp)
            await session.commit()

        # Patch session factory in EVERY module that imported it as a bare
        # name at load time. Bindings are per-module in Python, so patching
        # db.session alone doesn't reach the scheduler's copy.
        import social_signal.db.session as db_session
        import social_signal.scheduler.queue as q

        def patched_factory():
            return factory

        originals: dict = {}
        for mod in (db_session, q):
            if hasattr(mod, "_session_factory"):
                originals[mod] = mod._session_factory
                mod._session_factory = patched_factory

        try:
            await q._promote_veto_windows()
        finally:
            for mod, orig in originals.items():
                mod._session_factory = orig

        async with factory() as session:
            from sqlmodel import select
            result = await session.execute(
                select(ScheduledPost).where(ScheduledPost.id == sp_id)
            )
            updated = result.scalar_one_or_none()

        assert updated is not None
        assert updated.status == "queued"


# ---------------------------------------------------------------------------
# 4b. Publisher idempotency guard — if PublishedPost exists, do not re-publish
# ---------------------------------------------------------------------------


class TestPublisherIdempotencyGuard:
    """If a PublishedPost already exists for a ScheduledPost, publish() must
    short-circuit to status=done and never invoke the platform publisher."""

    @pytest.mark.asyncio
    async def test_existing_published_post_short_circuits(self):
        from datetime import datetime

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.orm import sessionmaker
        from sqlmodel import SQLModel, select
        from sqlmodel.ext.asyncio.session import AsyncSession

        from social_signal.db.models import PublishedPost, ScheduledPost, VideoAsset

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        now = datetime.now(UTC).replace(tzinfo=None)
        asset_id = str(uuid.uuid4())
        sp_id = str(uuid.uuid4())
        pp_id = str(uuid.uuid4())

        async with factory() as session:
            session.add(VideoAsset(
                id=asset_id,
                script_id=str(uuid.uuid4()),
                file_path="/tmp/x.mp4",
                duration_s=10.0,
                created_at=now,
            ))
            session.add(ScheduledPost(
                id=sp_id,
                asset_id=asset_id,
                platform="zernio_tiktok",
                scheduled_for=now,
                status="queued",
                attempts=1,
            ))
            session.add(PublishedPost(
                id=pp_id,
                scheduled_post_id=sp_id,
                platform="zernio_tiktok",
                platform_post_id="already-live-id",
                platform_url="https://tiktok.com/@x/video/already",
                published_at=now,
            ))
            await session.commit()

        # Patch _session_factory in every module that bound it at import time.
        import social_signal.agent.nodes.publisher as pub
        import social_signal.db.session as db_session

        def patched_factory():
            return factory

        originals: dict = {}
        for mod in (db_session, pub):
            if hasattr(mod, "_session_factory"):
                originals[mod] = mod._session_factory
                mod._session_factory = patched_factory

        # Platform dispatcher must NOT be called on the short-circuit path.
        async def must_not_call(*args, **kwargs):
            raise AssertionError("_publish_to_platform must not run when PublishedPost exists")
        original_dispatch = pub._publish_to_platform
        pub._publish_to_platform = must_not_call

        try:
            await pub.publish(sp_id)
        finally:
            pub._publish_to_platform = original_dispatch
            for mod, orig in originals.items():
                mod._session_factory = orig

        async with factory() as session:
            result = await session.execute(
                select(ScheduledPost).where(ScheduledPost.id == sp_id)
            )
            updated = result.scalar_one_or_none()
            # Count PublishedPost rows — must stay at 1, no duplicate row.
            pp_count = (await session.execute(
                select(PublishedPost).where(PublishedPost.scheduled_post_id == sp_id)
            )).scalars().all()

        assert updated is not None
        assert updated.status == "done"
        assert len(pp_count) == 1


# ---------------------------------------------------------------------------
# 5. Classifier dry-run — returns positive tier
# ---------------------------------------------------------------------------

class TestClassifierDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_positive(self):
        from social_signal.orm.classifier import classify

        result = await classify("Great bot! Love the cobra.", "twitter")
        assert result["tier"] == "positive"
        assert result["confidence"] == 1.0


# ---------------------------------------------------------------------------
# 6. Server health check — startup without real DB
# ---------------------------------------------------------------------------

class TestServerHealth:
    @pytest.mark.asyncio
    async def test_healthz_returns_ok(self):
        """Test healthz endpoint structure using in-memory SQLite."""
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.orm import sessionmaker
        from sqlmodel import SQLModel
        from sqlmodel.ext.asyncio.session import AsyncSession

        import social_signal.db.models  # noqa: F401 — register metadata
        import social_signal.server as srv

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        # healthz calls _session_factory() and expects it to return a sessionmaker
        # that can be called to get a session context manager.
        original = srv._session_factory
        srv._session_factory = lambda: factory
        try:
            result = await srv.healthz()
        finally:
            srv._session_factory = original

        assert result["status"] == "ok"
        assert result["service"] == "social-signal"
        assert "queue" in result


# ---------------------------------------------------------------------------
# 7. Config loading
# ---------------------------------------------------------------------------

class TestConfig:
    def test_is_dry_run(self):
        from social_signal.config import settings
        s = settings()
        assert s.is_dry_run is True  # set at top of this file

    def test_brand_config_loads_defaults(self):
        from social_signal.config import brand_config
        bc = brand_config()
        assert bc["brand"]["accent_color"] == "#00ff88"
        assert bc["brand"]["base_color"] == "#0a0a0f"
        assert "hard_stop_phrases" in bc["orm_guardrails"]

    def test_github_repo_list(self):
        from social_signal.config import Settings
        s = Settings(github_repos="glitch-cod-confirm,ai_marketing_stack-ads-agent")
        assert s.github_repo_list == ["glitch-cod-confirm", "ai_marketing_stack-ads-agent"]
