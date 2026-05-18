"""Upload-Post analytics sweep — metric mapping, eligibility, idempotency."""
from __future__ import annotations

import json
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


def _write_brand(configs_dir: pathlib.Path, brand_id: str, user: str = "TestUser") -> None:
    cfg = {
        "brand_id": brand_id,
        "display_name": brand_id,
        "timezone": "UTC",
        "platforms": {
            "upload_post_tiktok": {"enabled": True, "user": user},
        },
    }
    (configs_dir / f"{brand_id}.json").write_text(json.dumps(cfg))


async def _build_test_db():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    import glitch_signal.analytics.upload_post as an
    import glitch_signal.db.session as db_session

    def _getter():
        return factory

    originals = {}
    for mod in (db_session, an):
        if hasattr(mod, "_session_factory"):
            originals[mod] = mod._session_factory
            mod._session_factory = _getter
    return factory, originals


def _restore(originals: dict) -> None:
    for mod, orig in originals.items():
        mod._session_factory = orig


# ---------------------------------------------------------------------------
# extract_metrics — defensive key coalescing
# ---------------------------------------------------------------------------

class TestExtractMetrics:
    def test_flat_shape(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics({"views": 1200, "likes": 45, "comments": 3, "shares": 7}, "tiktok")
        assert m == {"views": 1200, "likes": 45, "comments": 3, "shares": 7}

    def test_nested_metrics_key(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics({"metrics": {"view_count": 500, "like_count": 10}}, "tiktok")
        assert m["views"] == 500
        assert m["likes"] == 10
        assert m["comments"] == 0

    def test_platform_keyed_shape(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics(
            {"tiktok": {"play_count": 9000, "favorite_count": 100}},
            "tiktok",
        )
        assert m["views"] == 9000
        assert m["likes"] == 100

    def test_youtube_aliases(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics({"video_views": 5, "favorites": 2, "replies": 1}, "youtube")
        assert m["views"] == 5
        assert m["likes"] == 2
        assert m["comments"] == 1

    def test_unknown_keys_yield_zero(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics({"nothing_known": 42}, "tiktok")
        assert m == {"views": 0, "likes": 0, "comments": 0, "shares": 0}

    def test_non_numeric_values_ignored(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics({"views": "not a number", "likes": True, "shares": 5}, "tiktok")
        # "not a number" rejected, True rejected (bool is int subclass but we
        # explicitly guard), 5 accepted.
        assert m["views"] == 0
        assert m["likes"] == 0
        assert m["shares"] == 5

    def test_non_dict_payload_safe(self):
        from glitch_signal.analytics.upload_post import extract_metrics
        m = extract_metrics(None, "tiktok")
        assert m == {"views": 0, "likes": 0, "comments": 0, "shares": 0}


# ---------------------------------------------------------------------------
# canonical platform helper
# ---------------------------------------------------------------------------

class TestCanonicalPlatform:
    def test_upload_post_prefix_stripped(self):
        from glitch_signal.analytics.upload_post import _canonical_platform
        assert _canonical_platform("upload_post_tiktok") == "tiktok"
        assert _canonical_platform("upload_post_youtube") == "youtube"

    def test_non_upload_post_returns_none(self):
        from glitch_signal.analytics.upload_post import _canonical_platform
        assert _canonical_platform("zernio_tiktok") is None
        assert _canonical_platform("tiktok") is None


# ---------------------------------------------------------------------------
# sweep_due_posts
# ---------------------------------------------------------------------------

class TestSweepEligibility:
    @pytest.mark.asyncio
    async def test_pulls_once_for_post_with_no_snapshot(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.analytics.upload_post import sweep_due_posts
        from glitch_signal.db.models import MetricsSnapshot, PublishedPost

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "bnd", user="MyBrand")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "bnd")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            pp_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(PublishedPost(
                    id=pp_id,
                    brand_id="bnd",
                    scheduled_post_id=str(uuid.uuid4()),
                    platform="upload_post_tiktok",
                    platform_post_id="76297",
                    platform_url="https://tt/x/video/76297",
                    published_at=now - timedelta(hours=3),
                ))
                await session.commit()

            captured = {}

            async def fake_fetch(platform_post_id, platform, user):
                captured["ppid"] = platform_post_id
                captured["platform"] = platform
                captured["user"] = user
                return {"views": 1200, "likes": 45, "comments": 3, "shares": 7}

            monkeypatch.setattr(
                "glitch_signal.analytics.upload_post.fetch_metrics_for_post",
                fake_fetch,
            )

            updated = await sweep_due_posts()
            assert updated == [pp_id]
            assert captured == {"ppid": "76297", "platform": "tiktok", "user": "MyBrand"}

            async with factory() as session:
                from sqlalchemy import select
                rows = (await session.execute(
                    select(MetricsSnapshot).where(
                        MetricsSnapshot.published_post_id == pp_id
                    )
                )).scalars().all()
            assert len(rows) == 1
            assert rows[0].views == 1200
            assert rows[0].likes == 45
            assert rows[0].comments == 3
            assert rows[0].shares == 7
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_skips_posts_inside_first_pull_grace(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.analytics.upload_post import sweep_due_posts
        from glitch_signal.db.models import PublishedPost

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "bnd2")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "bnd2")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        # Grace = 1 hour (default), post is 5 minutes old → not eligible.
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            async with factory() as session:
                session.add(PublishedPost(
                    id=str(uuid.uuid4()),
                    brand_id="bnd2",
                    scheduled_post_id=str(uuid.uuid4()),
                    platform="upload_post_tiktok",
                    platform_post_id="fresh",
                    published_at=now - timedelta(minutes=5),
                ))
                await session.commit()

            async def must_not_fetch(*a, **kw):
                raise AssertionError("fetch should not run inside first-pull grace")
            monkeypatch.setattr(
                "glitch_signal.analytics.upload_post.fetch_metrics_for_post",
                must_not_fetch,
            )

            updated = await sweep_due_posts()
            assert updated == []
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_skips_posts_with_recent_snapshot(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.analytics.upload_post import sweep_due_posts
        from glitch_signal.db.models import MetricsSnapshot, PublishedPost

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "bnd3")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "bnd3")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        # interval = 24h (default); most recent snapshot = 1h ago → not due yet.
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            pp_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(PublishedPost(
                    id=pp_id,
                    brand_id="bnd3",
                    scheduled_post_id=str(uuid.uuid4()),
                    platform="upload_post_tiktok",
                    platform_post_id="x",
                    published_at=now - timedelta(hours=48),
                ))
                session.add(MetricsSnapshot(
                    id=str(uuid.uuid4()),
                    brand_id="bnd3",
                    published_post_id=pp_id,
                    captured_at=now - timedelta(hours=1),
                    views=10, likes=1, comments=0, shares=0,
                ))
                await session.commit()

            async def must_not_fetch(*a, **kw):
                raise AssertionError("fetch should not run; snapshot is fresh")
            monkeypatch.setattr(
                "glitch_signal.analytics.upload_post.fetch_metrics_for_post",
                must_not_fetch,
            )

            updated = await sweep_due_posts()
            assert updated == []
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_re_pulls_when_snapshot_is_stale(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.analytics.upload_post import sweep_due_posts
        from glitch_signal.db.models import MetricsSnapshot, PublishedPost

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "bnd4")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "bnd4")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            pp_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(PublishedPost(
                    id=pp_id,
                    brand_id="bnd4",
                    scheduled_post_id=str(uuid.uuid4()),
                    platform="upload_post_tiktok",
                    platform_post_id="y",
                    published_at=now - timedelta(days=5),
                ))
                # Snapshot 25h ago → older than 24h interval.
                session.add(MetricsSnapshot(
                    id=str(uuid.uuid4()),
                    brand_id="bnd4",
                    published_post_id=pp_id,
                    captured_at=now - timedelta(hours=25),
                    views=100, likes=5, comments=0, shares=0,
                ))
                await session.commit()

            async def fake_fetch(platform_post_id, platform, user):
                return {"views": 200, "likes": 12, "comments": 1, "shares": 2}
            monkeypatch.setattr(
                "glitch_signal.analytics.upload_post.fetch_metrics_for_post",
                fake_fetch,
            )

            updated = await sweep_due_posts()
            assert updated == [pp_id]

            async with factory() as session:
                from sqlalchemy import select
                rows = (await session.execute(
                    select(MetricsSnapshot).where(
                        MetricsSnapshot.published_post_id == pp_id
                    )
                )).scalars().all()
            # Both old + new snapshots preserved so the agent can compute deltas.
            assert len(rows) == 2
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_skips_non_upload_post_platforms(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.analytics.upload_post import sweep_due_posts
        from glitch_signal.db.models import PublishedPost

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "bnd5")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "bnd5")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            async with factory() as session:
                # Zernio-published — analytics sweep doesn't know how to fetch these yet.
                session.add(PublishedPost(
                    id=str(uuid.uuid4()),
                    brand_id="bnd5",
                    scheduled_post_id=str(uuid.uuid4()),
                    platform="zernio_tiktok",
                    platform_post_id="z",
                    published_at=now - timedelta(hours=3),
                ))
                await session.commit()

            async def must_not_fetch(*a, **kw):
                raise AssertionError("zernio-backed posts are out of scope")
            monkeypatch.setattr(
                "glitch_signal.analytics.upload_post.fetch_metrics_for_post",
                must_not_fetch,
            )

            updated = await sweep_due_posts()
            assert updated == []
        finally:
            _restore(originals)

    @pytest.mark.asyncio
    async def test_fetch_error_does_not_write_snapshot(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.analytics.upload_post import sweep_due_posts
        from glitch_signal.db.models import MetricsSnapshot, PublishedPost

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "bnd6")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "bnd6")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory, originals = await _build_test_db()
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            pp_id = str(uuid.uuid4())
            async with factory() as session:
                session.add(PublishedPost(
                    id=pp_id,
                    brand_id="bnd6",
                    scheduled_post_id=str(uuid.uuid4()),
                    platform="upload_post_tiktok",
                    platform_post_id="e",
                    published_at=now - timedelta(hours=3),
                ))
                await session.commit()

            async def raising_fetch(*a, **kw):
                raise RuntimeError("upstream 503")
            monkeypatch.setattr(
                "glitch_signal.analytics.upload_post.fetch_metrics_for_post",
                raising_fetch,
            )

            updated = await sweep_due_posts()
            assert updated == []

            async with factory() as session:
                from sqlalchemy import select
                rows = (await session.execute(
                    select(MetricsSnapshot).where(
                        MetricsSnapshot.published_post_id == pp_id
                    )
                )).scalars().all()
            assert rows == []
        finally:
            _restore(originals)
