"""Upload-Post inbound webhook — dispatch, idempotency, account events."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

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
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()


async def _build_test_db():
    """Spin up an in-memory DB and rewire every module that bound
    _session_factory at import time."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    import glitch_signal.agent.nodes.publisher as pub
    import glitch_signal.db.session as db_session
    import glitch_signal.scheduler.queue as q
    import glitch_signal.webhooks.upload_post as wh

    def _factory_getter():
        return factory

    originals: dict = {}
    for mod in (db_session, pub, q, wh):
        if hasattr(mod, "_session_factory"):
            originals[mod] = mod._session_factory
            mod._session_factory = _factory_getter
    return factory, originals


def _restore(originals: dict) -> None:
    for mod, orig in originals.items():
        mod._session_factory = orig


# ---------------------------------------------------------------------------
# upload_completed
# ---------------------------------------------------------------------------

class TestUploadCompleted:
    @pytest.mark.asyncio
    async def test_finalizes_scheduled_post_and_writes_published_post(self):
        from sqlmodel import select

        from glitch_signal.db.models import (
            PublishedPost,
            ScheduledPost,
            VideoAsset,
        )
        from glitch_signal.webhooks.upload_post import dispatch

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())
            request_id = "req-ABC123"

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
                    brand_id="drive_brand",
                    asset_id=asset_id,
                    platform="upload_post_tiktok",
                    scheduled_for=now,
                    status="awaiting_webhook",
                    attempts=1,
                    vendor_request_id=request_id,
                ))
                await session.commit()

            event = {
                "event": "upload_completed",
                "job_id": request_id,
                "user": "MyBrand",
                "platform": "tiktok",
                "results": [{
                    "platform": "tiktok",
                    "success": True,
                    "platform_post_id": "7629763358610574613",
                    "post_url": "https://www.tiktok.com/@x/video/7629763358610574613",
                }],
            }
            result = await dispatch(event)
            assert result["ok"] is True
            assert result["handled"] is True
            assert result["scheduled_post_id"] == sp_id

            async with factory() as session:
                sp_row = await session.get(ScheduledPost, sp_id)
                assert sp_row.status == "done"

                pubs = (await session.execute(
                    select(PublishedPost).where(
                        PublishedPost.scheduled_post_id == sp_id
                    )
                )).scalars().all()
                assert len(pubs) == 1
                assert pubs[0].platform_post_id == "7629763358610574613"
                assert pubs[0].platform_url.startswith("https://www.tiktok.com/")
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_duplicate_webhook_is_idempotent(self):
        from sqlmodel import select

        from glitch_signal.db.models import (
            PublishedPost,
            ScheduledPost,
            VideoAsset,
        )
        from glitch_signal.webhooks.upload_post import dispatch

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())
            request_id = "req-DUP"

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
                    brand_id="drive_brand",
                    asset_id=asset_id,
                    platform="upload_post_tiktok",
                    scheduled_for=now,
                    status="awaiting_webhook",
                    attempts=1,
                    vendor_request_id=request_id,
                ))
                await session.commit()

            event = {
                "event": "upload_completed",
                "job_id": request_id,
                "results": [{
                    "platform": "tiktok",
                    "platform_post_id": "X-POST-ID",
                    "post_url": "https://tt/x/video/X",
                }],
            }
            r1 = await dispatch(event)
            r2 = await dispatch(event)
            assert r1["handled"] is True
            assert r2["handled"] is True
            assert r2.get("duplicate") is True

            async with factory() as session:
                pubs = (await session.execute(
                    select(PublishedPost).where(
                        PublishedPost.scheduled_post_id == sp_id
                    )
                )).scalars().all()
                assert len(pubs) == 1, f"expected 1 PublishedPost, got {len(pubs)}"
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_failed_publish_marks_scheduled_post_failed(self):
        from glitch_signal.db.models import ScheduledPost, VideoAsset
        from glitch_signal.webhooks.upload_post import dispatch

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())
            request_id = "req-FAIL"

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
                    brand_id="drive_brand",
                    asset_id=asset_id,
                    platform="upload_post_tiktok",
                    scheduled_for=now,
                    status="awaiting_webhook",
                    attempts=1,
                    vendor_request_id=request_id,
                ))
                await session.commit()

            event = {
                "event": "upload_completed",
                "job_id": request_id,
                "results": [{
                    "platform": "tiktok",
                    "success": False,
                    "error_message": "video rejected by TikTok: music not allowed",
                }],
            }
            result = await dispatch(event)
            assert result["handled"] is True
            assert result["status"] == "failed"

            async with factory() as session:
                sp_row = await session.get(ScheduledPost, sp_id)
                assert sp_row.status == "failed"
                assert "music not allowed" in (sp_row.last_error or "")
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_unknown_request_id_logs_but_returns_ok(self):
        from glitch_signal.webhooks.upload_post import dispatch

        factory, originals = await _build_test_db()
        try:
            event = {
                "event": "upload_completed",
                "job_id": "never-seen",
                "results": [{"platform": "tiktok",
                             "platform_post_id": "x",
                             "post_url": "https://tt"}],
            }
            r = await dispatch(event)
            assert r["ok"] is True
            assert r["handled"] is False
        finally:
            _restore(originals)


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------

class TestAccountEvents:
    @pytest.mark.asyncio
    async def test_reauth_required_flips_platform_auth_status(self):
        from glitch_signal.db.models import PlatformAuth
        from glitch_signal.webhooks.upload_post import dispatch

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            pa_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(PlatformAuth(
                    id=pa_id,
                    brand_id="drive_brand",
                    platform="tiktok",
                    account_identifier="acc-123",
                    access_token_enc="fake",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ))
                await session.commit()

            event = {
                "event": "social_account_reauth_required",
                "user": "MyBrand",
                "platform": "tiktok",
                "account_id": "acc-123",
                "reason": "token_refresh_threshold_exceeded",
            }
            r = await dispatch(event)
            assert r["handled"] is True
            assert r["new_status"] == "needs_reauth"

            async with factory() as session:
                row = await session.get(PlatformAuth, pa_id)
                assert row.status == "needs_reauth"
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_disconnected_marks_revoked(self):
        from glitch_signal.db.models import PlatformAuth
        from glitch_signal.webhooks.upload_post import dispatch

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            pa_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(PlatformAuth(
                    id=pa_id,
                    brand_id="drive_brand",
                    platform="tiktok",
                    account_identifier="acc-X",
                    access_token_enc="fake",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ))
                await session.commit()

            event = {
                "event": "social_account_disconnected",
                "platform": "tiktok",
                "account_id": "acc-X",
                "reason": "manual_disconnect",
            }
            r = await dispatch(event)
            assert r["new_status"] == "revoked"

            async with factory() as session:
                row = await session.get(PlatformAuth, pa_id)
                assert row.status == "revoked"
        finally:
            _restore(originals)


# ---------------------------------------------------------------------------
# Unknown / malformed events
# ---------------------------------------------------------------------------

class TestUnknownEvents:
    @pytest.mark.asyncio
    async def test_unknown_event_type_returns_ok_handled_false(self):
        from glitch_signal.webhooks.upload_post import dispatch

        r = await dispatch({"event": "something_new", "data": {}})
        assert r["ok"] is True
        assert r["handled"] is False

    @pytest.mark.asyncio
    async def test_missing_event_type_returns_ok_false(self):
        from glitch_signal.webhooks.upload_post import dispatch

        r = await dispatch({"no_event_key": True})
        assert r["ok"] is False
        assert r["handled"] is False


# ---------------------------------------------------------------------------
# HTTP endpoint — secret-as-URL-segment access control
# ---------------------------------------------------------------------------

class TestWebhookHTTPEndpoint:
    @pytest.mark.asyncio
    async def test_bad_secret_returns_403(self, monkeypatch):
        from fastapi import HTTPException

        from glitch_signal import config as cfg
        from glitch_signal.server import upload_post_webhook

        monkeypatch.setenv("UPLOAD_POST_WEBHOOK_SECRET", "correct-secret")
        cfg.settings.cache_clear()

        class _FakeReq:
            async def json(self):
                return {"event": "upload_completed"}

        with pytest.raises(HTTPException) as exc:
            await upload_post_webhook(secret="wrong-secret", request=_FakeReq())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_config_returns_503(self, monkeypatch):
        from fastapi import HTTPException

        from glitch_signal import config as cfg
        from glitch_signal.server import upload_post_webhook

        monkeypatch.setenv("UPLOAD_POST_WEBHOOK_SECRET", "")
        cfg.settings.cache_clear()

        class _FakeReq:
            async def json(self):
                return {"event": "upload_completed"}

        with pytest.raises(HTTPException) as exc:
            await upload_post_webhook(secret="anything", request=_FakeReq())
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_good_secret_dispatches(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.server import upload_post_webhook

        monkeypatch.setenv("UPLOAD_POST_WEBHOOK_SECRET", "good-secret")
        cfg.settings.cache_clear()

        captured = {}

        async def fake_dispatch(event):
            captured["event"] = event
            return {"ok": True, "handled": False, "event": event.get("event")}

        monkeypatch.setattr(
            "glitch_signal.webhooks.upload_post.dispatch", fake_dispatch
        )

        class _FakeReq:
            async def json(self):
                return {"event": "upload_completed", "job_id": "r-1"}

        r = await upload_post_webhook(secret="good-secret", request=_FakeReq())
        assert r["ok"] is True
        assert captured["event"]["job_id"] == "r-1"


# ---------------------------------------------------------------------------
# Publisher integration — webhook_pending sentinel
# ---------------------------------------------------------------------------

class TestReconciliationSweep:
    """If the webhook is dropped / our server was down during callback,
    _reconcile_awaiting_webhook polls get_status and finalizes the row."""

    @pytest.mark.asyncio
    async def test_sweep_finalizes_via_get_status(self, monkeypatch, tmp_path):
        from datetime import timedelta

        from sqlmodel import select

        from glitch_signal import config as cfg
        from glitch_signal.db.models import (
            PublishedPost,
            ScheduledPost,
            VideoAsset,
        )
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        monkeypatch.setenv("UPLOAD_POST_WEBHOOK_RECONCILE_AFTER_S", "60")
        cfg.settings.cache_clear()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())
            request_id = "req-RECON"

            async with factory() as session:
                session.add(VideoAsset(
                    id=asset_id,
                    script_id=str(uuid.uuid4()),
                    file_path="/tmp/x.mp4",
                    duration_s=10.0,
                    created_at=now,
                ))
                # last_attempt_at is 30 min ago → well past reconcile window.
                session.add(ScheduledPost(
                    id=sp_id,
                    brand_id="drive_brand",
                    asset_id=asset_id,
                    platform="upload_post_tiktok",
                    scheduled_for=now,
                    status="awaiting_webhook",
                    attempts=1,
                    vendor_request_id=request_id,
                    last_attempt_at=now - timedelta(minutes=30),
                ))
                await session.commit()

            async def fake_poll(req_id, target):
                assert req_id == request_id
                assert target == "tiktok"
                return "7629000", "https://tt/x/video/7629000"

            monkeypatch.setattr(
                "glitch_signal.platforms.upload_post.poll_status_for_request",
                fake_poll,
            )

            await q._reconcile_awaiting_webhook()

            async with factory() as session:
                sp_row = await session.get(ScheduledPost, sp_id)
                assert sp_row.status == "done"
                pubs = (await session.execute(
                    select(PublishedPost).where(
                        PublishedPost.scheduled_post_id == sp_id
                    )
                )).scalars().all()
                assert len(pubs) == 1
                assert pubs[0].platform_post_id == "7629000"
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_sweep_updates_sheet_row_to_posted(self, monkeypatch):
        """Regression for 2026-04-18: reconciliation wrote PublishedPost
        but forgot to flip the brand's Google Sheet row from `captioned`
        to `posted`. 11 Namhya posts went live but sheet stayed stale."""
        from datetime import timedelta

        from glitch_signal import config as cfg
        from glitch_signal.db.models import (
            ContentScript,
            ScheduledPost,
            Signal,
            VideoAsset,
        )
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        monkeypatch.setenv("UPLOAD_POST_WEBHOOK_RECONCILE_AFTER_S", "60")
        cfg.settings.cache_clear()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            sig_id = str(uuid.uuid4())
            cs_id = str(uuid.uuid4())
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(Signal(
                    id=sig_id, brand_id="drive_brand", source="drive",
                    source_ref="FID1",
                    summary="Drive clip: Liver_ad15.mp4",
                    novelty_score=1.0, status="scripted", created_at=now,
                ))
                session.add(ContentScript(
                    id=cs_id, brand_id="drive_brand", signal_id=sig_id,
                    platform="tiktok", script_body="", content_type="drive",
                    status="done", created_at=now,
                ))
                session.add(VideoAsset(
                    id=asset_id, brand_id="drive_brand", script_id=cs_id,
                    file_path="/tmp/x.mp4", duration_s=1.0, created_at=now,
                ))
                session.add(ScheduledPost(
                    id=sp_id, brand_id="drive_brand", asset_id=asset_id,
                    platform="upload_post_tiktok", scheduled_for=now,
                    status="awaiting_webhook", attempts=1,
                    vendor_request_id="req-abc",
                    last_attempt_at=now - timedelta(minutes=30),
                ))
                await session.commit()

            async def fake_poll(req_id, target):
                return "TT-POST-1", "https://tiktok.com/x/video/1"
            monkeypatch.setattr(
                "glitch_signal.platforms.upload_post.poll_status_for_request",
                fake_poll,
            )

            captured_sheet = {}
            async def fake_update(brand_id, video_name, updates):
                captured_sheet["brand_id"] = brand_id
                captured_sheet["video_name"] = video_name
                captured_sheet["updates"] = updates
                return True
            monkeypatch.setattr(
                "glitch_signal.integrations.sheet_tracker.update_by_video_name",
                fake_update,
            )

            await q._reconcile_awaiting_webhook()

            assert captured_sheet["brand_id"] == "drive_brand"
            assert captured_sheet["video_name"] == "Liver_ad15.mp4"
            assert captured_sheet["updates"]["status"] == "posted"
            assert captured_sheet["updates"]["tiktok_url"] == "https://tiktok.com/x/video/1"
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_sweep_skips_rows_inside_reconcile_window(self, monkeypatch):
        from datetime import timedelta

        from glitch_signal import config as cfg
        from glitch_signal.db.models import ScheduledPost, VideoAsset
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        # Window = 600s. last_attempt_at = 60s ago → inside window → skip.
        monkeypatch.setenv("UPLOAD_POST_WEBHOOK_RECONCILE_AFTER_S", "600")
        cfg.settings.cache_clear()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())

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
                    brand_id="drive_brand",
                    asset_id=asset_id,
                    platform="upload_post_tiktok",
                    scheduled_for=now,
                    status="awaiting_webhook",
                    attempts=1,
                    vendor_request_id="req-fresh",
                    last_attempt_at=now - timedelta(seconds=60),
                ))
                await session.commit()

            async def fake_poll(req_id, target):
                raise AssertionError(
                    "poll_status_for_request must NOT be called inside reconcile window"
                )

            monkeypatch.setattr(
                "glitch_signal.platforms.upload_post.poll_status_for_request",
                fake_poll,
            )

            await q._reconcile_awaiting_webhook()

            async with factory() as session:
                sp_row = await session.get(ScheduledPost, sp_id)
                assert sp_row.status == "awaiting_webhook"
        finally:
            _restore(originals)


class TestPublisherWebhookPendingSentinel:
    @pytest.mark.asyncio
    async def test_sentinel_persists_request_id_and_sets_awaiting_webhook(self):
        from glitch_signal.agent.nodes import publisher as pub
        from glitch_signal.db.models import ScheduledPost, VideoAsset

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            asset_id = str(uuid.uuid4())
            sp_id = str(uuid.uuid4())

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
                    brand_id="drive_brand",
                    asset_id=asset_id,
                    platform="upload_post_tiktok",
                    scheduled_for=now,
                    status="queued",
                    attempts=0,
                ))
                await session.commit()

            async def fake_dispatch(platform, file_path, script_id, brand_id=None, attempts=1):
                return "webhook_pending:REQ-XYZ", None

            # Stub pre-publish hooks so this test doesn't need a real file
            # on disk — we're exercising publisher.py's sentinel handling,
            # not the transform or JIT-download pipelines.
            import glitch_signal.media.ffmpeg as ffmpeg_mod
            async def passthrough(file_path, brand_id, platform_key):
                return file_path
            async def noop_ensure(asset):
                return None
            original_dispatch = pub._publish_to_platform
            original_apply = ffmpeg_mod.apply_transforms
            original_ensure = pub._ensure_local_file
            pub._publish_to_platform = fake_dispatch
            ffmpeg_mod.apply_transforms = passthrough
            pub._ensure_local_file = noop_ensure
            try:
                await pub.publish(sp_id)
            finally:
                pub._publish_to_platform = original_dispatch
                ffmpeg_mod.apply_transforms = original_apply
                pub._ensure_local_file = original_ensure

            async with factory() as session:
                sp_row = await session.get(ScheduledPost, sp_id)
                assert sp_row.status == "awaiting_webhook"
                assert sp_row.vendor_request_id == "REQ-XYZ"
        finally:
            _restore(originals)
