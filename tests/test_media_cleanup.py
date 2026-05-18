"""Media cleanup tick + JIT download publisher hook.

Two concerns, one PR:
  - scheduler._cleanup_posted_media deletes local video + transform
    siblings N minutes after PublishedPost was written
  - publisher._ensure_local_file downloads from Drive on-demand when
    the asset's file_path doesn't yet exist
"""
from __future__ import annotations

import os
import pathlib
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
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()


async def _build_test_db():
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

    def _getter():
        return factory
    originals = {}
    for mod in (db_session, pub, q):
        if hasattr(mod, "_session_factory"):
            originals[mod] = mod._session_factory
            mod._session_factory = _getter
    return factory, originals


def _restore(originals):
    for mod, orig in originals.items():
        mod._session_factory = orig


async def _seed_published(
    factory, *, tmp_path: pathlib.Path, published_min_ago: int,
    with_transform: bool = False,
):
    """Create a PublishedPost + ScheduledPost + VideoAsset pointing at a real file.

    Returns (asset_path, transform_path_or_None).
    """
    from glitch_signal.db.models import (
        PublishedPost,
        ScheduledPost,
        VideoAsset,
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    asset_path = tmp_path / "clip.mp4"
    asset_path.write_bytes(b"v" * 1024)   # 1KB sentinel bytes
    transform_path = None
    if with_transform:
        transform_path = tmp_path / "clip.strip_audio.mp4"
        transform_path.write_bytes(b"t" * 512)

    asset_id = str(uuid.uuid4())
    sp_id = str(uuid.uuid4())
    pp_id = str(uuid.uuid4())

    async with factory() as session:
        session.add(VideoAsset(
            id=asset_id, script_id=str(uuid.uuid4()),
            file_path=str(asset_path), duration_s=10.0, created_at=now,
        ))
        session.add(ScheduledPost(
            id=sp_id, brand_id="b", asset_id=asset_id,
            platform="upload_post_tiktok",
            scheduled_for=now - timedelta(minutes=published_min_ago),
            status="done",
        ))
        session.add(PublishedPost(
            id=pp_id, brand_id="b", scheduled_post_id=sp_id,
            platform="upload_post_tiktok",
            platform_post_id="TT-1", platform_url="https://tt/x",
            published_at=now - timedelta(minutes=published_min_ago),
        ))
        await session.commit()

    return asset_path, transform_path


class TestCleanupWindow:
    @pytest.mark.asyncio
    async def test_deletes_after_window_elapsed(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "30")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, _ = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=60,
            )
            assert asset_path.exists()
            await q._cleanup_posted_media()
            assert not asset_path.exists()
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_keeps_inside_window(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "60")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, _ = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=15,
            )
            await q._cleanup_posted_media()
            assert asset_path.exists()   # still inside 60 min window
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_zero_window_disables_cleanup(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "0")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, _ = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=9999,
            )
            await q._cleanup_posted_media()
            assert asset_path.exists()
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_idempotent(self, tmp_path, monkeypatch):
        """Running twice doesn't raise even though file is already gone."""
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "1")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, _ = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=60,
            )
            await q._cleanup_posted_media()
            await q._cleanup_posted_media()
            assert not asset_path.exists()
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_missing_file_noop(self, tmp_path, monkeypatch):
        """If the file was already removed manually, cleanup is a no-op."""
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "1")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, _ = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=60,
            )
            asset_path.unlink()
            # Should complete without error
            await q._cleanup_posted_media()
        finally:
            _restore(originals)


class TestTransformSiblings:
    @pytest.mark.asyncio
    async def test_strip_audio_sibling_also_deleted(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "1")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, transform_path = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=60,
                with_transform=True,
            )
            assert asset_path.exists()
            assert transform_path.exists()
            await q._cleanup_posted_media()
            assert not asset_path.exists()
            assert not transform_path.exists()
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_unrelated_sibling_preserved(self, tmp_path, monkeypatch):
        """A file sharing the stem but with an unknown middle segment
        (e.g. .backup.mp4) must NOT be deleted — only known transform
        names are in scope."""
        from glitch_signal import config as cfg
        from glitch_signal.scheduler import queue as q

        monkeypatch.setenv("MEDIA_CLEANUP_AFTER_MINUTES", "1")
        cfg.settings.cache_clear()
        factory, originals = await _build_test_db()
        try:
            asset_path, _ = await _seed_published(
                factory, tmp_path=tmp_path, published_min_ago=60,
            )
            unrelated = tmp_path / "clip.backup.mp4"
            unrelated.write_bytes(b"keep me")

            await q._cleanup_posted_media()
            assert not asset_path.exists()
            assert unrelated.exists()
        finally:
            _restore(originals)


class TestJITDownload:
    @pytest.mark.asyncio
    async def test_noop_when_file_already_exists(self, tmp_path, monkeypatch):
        """If the file is already on disk, no Drive call is made."""
        from glitch_signal.agent.nodes.publisher import _ensure_local_file
        from glitch_signal.db.models import VideoAsset

        async def must_not_download(*a, **kw):
            raise AssertionError("Drive download must not run when file exists")
        monkeypatch.setattr(
            "glitch_signal.integrations.google_drive.download_file",
            must_not_download,
        )

        path = tmp_path / "present.mp4"
        path.write_bytes(b"already here")
        asset = VideoAsset(
            id="a", script_id="s", file_path=str(path),
            duration_s=1.0,
        )
        await _ensure_local_file(asset)   # should not raise

    @pytest.mark.asyncio
    async def test_downloads_when_missing(self, tmp_path, monkeypatch):
        from glitch_signal.agent.nodes.publisher import _ensure_local_file
        from glitch_signal.db.models import ContentScript, Signal, VideoAsset

        factory, originals = await _build_test_db()
        try:
            asset_path = tmp_path / "to_download.mp4"
            async with factory() as session:
                session.add(Signal(
                    id="sig1", brand_id="b", source="drive",
                    source_ref="DRIVE-FILE-123",
                    summary="x", novelty_score=1.0, status="scripted",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                ))
                session.add(ContentScript(
                    id="cs1", brand_id="b", signal_id="sig1",
                    platform="tiktok", script_body="", content_type="drive",
                    status="done",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                ))
                asset = VideoAsset(
                    id="a1", brand_id="b", script_id="cs1",
                    file_path=str(asset_path), duration_s=0.0,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
                session.add(asset)
                await session.commit()

            downloads = []

            async def fake_download(file_id, dest):
                downloads.append((file_id, str(dest)))
                dest.write_bytes(b"fresh bytes")
                return len(b"fresh bytes")
            monkeypatch.setattr(
                "glitch_signal.integrations.google_drive.download_file",
                fake_download,
            )

            await _ensure_local_file(asset)
            assert asset_path.exists()
            assert downloads == [("DRIVE-FILE-123", str(asset_path))]
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_raises_when_no_drive_source(self, tmp_path, monkeypatch):
        """A file that's missing AND has no Drive source_ref can't be recovered."""
        from glitch_signal.agent.nodes.publisher import _ensure_local_file
        from glitch_signal.db.models import ContentScript, Signal, VideoAsset

        factory, originals = await _build_test_db()
        try:
            async with factory() as session:
                session.add(Signal(
                    id="sig1", brand_id="b", source="github",
                    source_ref="abc123",
                    summary="x", novelty_score=1.0, status="scripted",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                ))
                session.add(ContentScript(
                    id="cs1", brand_id="b", signal_id="sig1",
                    platform="tiktok", script_body="", content_type="drive",
                    status="done",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                ))
                asset = VideoAsset(
                    id="a1", brand_id="b", script_id="cs1",
                    file_path=str(tmp_path / "nope.mp4"), duration_s=0.0,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
                session.add(asset)
                await session.commit()

            with pytest.raises(FileNotFoundError, match="not Drive"):
                await _ensure_local_file(asset)
        finally:
            _restore(originals)
