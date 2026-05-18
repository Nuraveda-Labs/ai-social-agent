"""Upload-Post publisher — dry-run, platform-key routing, extras mapping."""
from __future__ import annotations

import os

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


class TestUploadPostPlatformMap:
    def test_known_platform_keys(self):
        from glitch_signal.platforms.upload_post import _PLATFORM_MAP
        assert _PLATFORM_MAP["upload_post_tiktok"] == "tiktok"
        assert _PLATFORM_MAP["upload_post_instagram"] == "instagram"
        assert _PLATFORM_MAP["upload_post_youtube"] == "youtube"


class TestUploadPostDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_fake_id_no_http(self):
        from glitch_signal.platforms import upload_post
        publish_id, url = await upload_post.publish(
            platform="upload_post_tiktok",
            file_path="/does/not/matter.mp4",
            script_id="s1",
            brand_id="drive_brand",
        )
        assert publish_id.startswith("uploadpost-dry-")
        assert url is None

    @pytest.mark.asyncio
    async def test_live_rejects_missing_api_key(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import upload_post

        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "")
        config.settings.cache_clear()

        with pytest.raises(RuntimeError, match="UPLOAD_POST_API_KEY"):
            await upload_post.publish(
                platform="upload_post_tiktok",
                file_path="/tmp/x.mp4",
                script_id="s",
                brand_id="drive_brand",
            )

    @pytest.mark.asyncio
    async def test_live_rejects_unknown_platform_key(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import upload_post

        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        config.settings.cache_clear()

        with pytest.raises(ValueError, match="unknown platform key"):
            await upload_post.publish(
                platform="upload_post_bogus",
                file_path="/tmp/x.mp4",
                script_id="s",
                brand_id="drive_brand",
            )


class TestPlatformExtras:
    def test_tiktok_extras_full_mapping(self):
        from glitch_signal.platforms.upload_post import _platform_extras
        cfg = {
            "default_privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": True,
            "disable_stitch": False,
            "disable_comment": False,
            "video_cover_timestamp_ms": 2500,
            "post_mode": "DIRECT_POST",
            "is_aigc": False,
        }
        e = _platform_extras("tiktok", cfg)
        assert e["privacy_level"] == "PUBLIC_TO_EVERYONE"
        assert e["disable_duet"] is True
        assert e["cover_timestamp"] == 2500
        assert e["post_mode"] == "DIRECT_POST"
        assert e["is_aigc"] is False

    def test_instagram_extras_default_reels(self):
        from glitch_signal.platforms.upload_post import _platform_extras
        e = _platform_extras("instagram", {})
        assert e["media_type"] == "REELS"

    def test_youtube_extras_default_public(self):
        from glitch_signal.platforms.upload_post import _platform_extras
        e = _platform_extras("youtube", {})
        assert e["privacyStatus"] == "public"
        assert e["categoryId"] == "22"

    def test_unknown_target_returns_empty(self):
        from glitch_signal.platforms.upload_post import _platform_extras
        assert _platform_extras("bluesky", {"anything": 1}) == {}


class TestCaptionFieldMapping:
    """Upload-Post's field semantics differ by platform:

    - Short-form (TikTok / IG / X / Threads / Bluesky): the SDK's `title`
      kwarg IS the caption body. `description` is a separate optional
      field that is silently ignored on these platforms — which is how
      nmahya's first live posts went up with an empty description column
      on TikTok. _submit_upload must send the caption as `title` for
      these platforms and skip `description`.
    - Long-form (YouTube / Pinterest / LinkedIn / Reddit): `title` is
      the real post title and `description` is the body.
    """

    def test_tiktok_caption_goes_into_title(self, monkeypatch):
        from glitch_signal.platforms import upload_post as up

        captured: dict = {}

        class _C:
            def upload_video(self, **kwargs):
                captured.update(kwargs)
                return {"success": True, "request_id": "r"}

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _C()}),
        )

        up._submit_upload(
            api_key="k", user="MyBrand", target_platform="tiktok",
            video_url="https://x/media/fetch?token=t",
            caption="Body text here #foo", title="Derived-title (should be ignored)",
            extras={},
        )
        assert captured.get("title") == "Body text here #foo", (
            f"tiktok caption must land in `title`, got {captured}"
        )
        assert "description" not in captured, (
            f"`description` silently drops on tiktok — don't send it, got {captured}"
        )

    def test_instagram_caption_goes_into_title(self, monkeypatch):
        from glitch_signal.platforms import upload_post as up

        captured: dict = {}

        class _C:
            def upload_video(self, **kwargs):
                captured.update(kwargs)
                return {"success": True, "request_id": "r"}

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _C()}),
        )

        up._submit_upload(
            api_key="k", user="MyBrand", target_platform="instagram",
            video_url="https://x/media/fetch?token=t",
            caption="IG caption with #hashtags", title="ignored",
            extras={},
        )
        assert captured.get("title") == "IG caption with #hashtags"
        assert "description" not in captured

    def test_youtube_call_includes_title(self, monkeypatch):
        from glitch_signal.platforms import upload_post as up

        captured: dict = {}

        class _C:
            def upload_video(self, **kwargs):
                captured.update(kwargs)
                return {"success": True, "request_id": "r"}

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _C()}),
        )

        up._submit_upload(
            api_key="k", user="MyBrand", target_platform="youtube",
            video_url="https://x/media/fetch?token=t",
            caption="Body text", title="Real YouTube Title",
            extras={},
        )
        assert captured.get("title") == "Real YouTube Title"


class TestPollStatusForRequest:
    """poll_status_for_request is a single-shot status poll used by the
    scheduler's reconciliation sweep when a webhook never arrived."""

    @pytest.mark.asyncio
    async def test_finds_post_url_from_results_list(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.platforms import upload_post as up

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()

        class _C:
            def get_status(self, request_id):
                return {"status": "completed", "results": [
                    {"platform": "tiktok", "success": True,
                     "platform_post_id": "762976", "post_url": "https://tt/x/video/1"}
                ]}

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _C()}),
        )

        ppid, url = await up.poll_status_for_request("req-123", "tiktok")
        assert ppid == "762976"
        assert url == "https://tt/x/video/1"

    @pytest.mark.asyncio
    async def test_in_flight_returns_none(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.platforms import upload_post as up

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()

        class _C:
            def get_status(self, request_id):
                return {"status": "processing", "results": [{"platform": "tiktok"}]}

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _C()}),
        )

        ppid, url = await up.poll_status_for_request("req", "tiktok")
        assert ppid is None and url is None

    @pytest.mark.asyncio
    async def test_platform_error_raises(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.platforms import upload_post as up

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()

        class _C:
            def get_status(self, request_id):
                return {"status": "completed", "results": [
                    {"platform": "tiktok", "success": False,
                     "error_message": "video rejected by TikTok"}
                ]}

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _C()}),
        )

        with pytest.raises(RuntimeError, match="video rejected"):
            await up.poll_status_for_request("req", "tiktok")


class TestResolvePublishPlatform:
    """Brand config drives which publisher wins. Upload-Post beats Zernio
    beats direct. If nothing is enabled, raise clearly."""

    def _write_brand(self, configs_dir, brand_id: str, platforms: dict):
        import json
        (configs_dir / f"{brand_id}.json").write_text(json.dumps({
            "brand_id": brand_id,
            "display_name": brand_id,
            "timezone": "UTC",
            "platforms": platforms,
        }))

    def test_upload_post_beats_zernio(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        self._write_brand(configs, "drive_brand", {
            "upload_post_tiktok": {"enabled": True, "user": "MyBrand"},
            "zernio_tiktok":      {"enabled": True, "account_id": "z1"},
            "tiktok":             {"enabled": True},
        })
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert cfg.resolve_publish_platform("drive_brand", "tiktok") == "upload_post_tiktok"

    def test_falls_back_to_zernio(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        self._write_brand(configs, "drive_brand", {
            "upload_post_tiktok": {"enabled": False},
            "zernio_tiktok":      {"enabled": True, "account_id": "z1"},
            "tiktok":             {"enabled": True},
        })
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert cfg.resolve_publish_platform("drive_brand", "tiktok") == "zernio_tiktok"

    def test_falls_back_to_direct(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        self._write_brand(configs, "drive_brand", {
            "upload_post_tiktok": {"enabled": False},
            "zernio_tiktok":      {"enabled": False},
            "tiktok":             {"enabled": True},
        })
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert cfg.resolve_publish_platform("drive_brand", "tiktok") == "tiktok"

    def test_raises_when_nothing_enabled(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        self._write_brand(configs, "drive_brand", {
            "tiktok":             {"enabled": False},
            "upload_post_tiktok": {"enabled": False},
        })
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(RuntimeError, match="no enabled publisher"):
            cfg.resolve_publish_platform("drive_brand", "tiktok")


class TestPublisherRoutesUploadPost:
    """publisher._publish_to_platform dispatches upload_post_* → platforms/upload_post."""

    @pytest.mark.asyncio
    async def test_routes_upload_post_tiktok(self, monkeypatch):
        from glitch_signal.agent.nodes import publisher

        captured = {}

        async def fake_publish(platform, file_path, script_id, brand_id=None, attempts=1):
            captured["platform"] = platform
            captured["brand_id"] = brand_id
            captured["attempts"] = attempts
            return "up-stub-id", "https://www.tiktok.com/@x/video/1"

        monkeypatch.setattr("glitch_signal.platforms.upload_post.publish", fake_publish)
        monkeypatch.setenv("DISPATCH_MODE", "live")
        from glitch_signal import config as cfg
        cfg.settings.cache_clear()

        post_id, url = await publisher._publish_to_platform(
            "upload_post_tiktok", "/x.mp4", "s1", brand_id="drive_brand", attempts=2
        )
        assert post_id == "up-stub-id"
        assert url == "https://www.tiktok.com/@x/video/1"
        assert captured == {"platform": "upload_post_tiktok", "brand_id": "drive_brand", "attempts": 2}


class TestUploadPostRetryShortCircuit:
    """On attempts > 1, publish() should peek at get_history and short-circuit
    if a matching post is already live, avoiding a duplicate upload."""

    @pytest.mark.asyncio
    async def test_retry_short_circuits_on_history_hit(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.platforms import upload_post as up

        # Brand config with upload_post_tiktok enabled.
        configs = tmp_path / "configs"
        configs.mkdir()
        import json
        (configs / "drive_brand.json").write_text(json.dumps({
            "brand_id": "drive_brand",
            "display_name": "MyBrand",
            "timezone": "UTC",
            "platforms": {"upload_post_tiktok": {"enabled": True, "user": "MyBrand"}},
        }))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        # Prepare a fake video file (publish checks existence).
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x")

        # Stub ContentScript loader so caption is deterministic.
        async def fake_read_caption(script_id, brand_id, cfg_block):
            return "Test caption", "Test title", []
        monkeypatch.setattr(up, "_read_caption", fake_read_caption)

        # Stub the upload_post SDK used by _lookup_recent_by_caption.
        class _FakeClient:
            def get_history(self, page=1, limit=20):
                return {"history": [
                    {"user": "MyBrand", "description": "Test caption",
                     "results": [{"platform": "tiktok",
                                  "platform_post_id": "7629763",
                                  "post_url": "https://tiktok.com/@x/video/7629763"}]}
                ]}
            def upload_video(self, **kwargs):
                raise AssertionError("upload_video should not be called on retry short-circuit")

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _FakeClient()}),
        )

        ppid, url = await up.publish(
            platform="upload_post_tiktok",
            file_path=str(video),
            script_id="s1",
            brand_id="drive_brand",
            attempts=2,
        )
        assert ppid == "7629763"
        assert url == "https://tiktok.com/@x/video/7629763"

    @pytest.mark.asyncio
    async def test_first_attempt_returns_webhook_pending_sentinel(self, tmp_path, monkeypatch):
        """On attempts=1 we upload and return a webhook_pending:<id> sentinel —
        NOT a finalized post_id. get_history must NOT be called (history-check
        is a retry-only concern) and get_status must NOT be called (finalization
        happens via webhook, not polling)."""
        from glitch_signal import config as cfg
        from glitch_signal.platforms import upload_post as up

        configs = tmp_path / "configs"
        configs.mkdir()
        import json
        (configs / "drive_brand.json").write_text(json.dumps({
            "brand_id": "drive_brand",
            "display_name": "MyBrand",
            "timezone": "UTC",
            "platforms": {"upload_post_tiktok": {"enabled": True, "user": "MyBrand"}},
        }))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x")

        async def fake_read_caption(script_id, brand_id, cfg_block):
            return "Test caption", "Test title", []
        monkeypatch.setattr(up, "_read_caption", fake_read_caption)

        upload_called = {"n": 0}

        class _FakeClient:
            def get_history(self, page=1, limit=20):
                raise AssertionError("get_history must not be called on first attempt")
            def upload_video(self, **kwargs):
                upload_called["n"] += 1
                return {"success": True, "request_id": "r-1"}
            def get_status(self, request_id):
                raise AssertionError(
                    "get_status must not be called from publish() — webhook-driven now"
                )

        import sys
        monkeypatch.setitem(
            sys.modules,
            "upload_post",
            type("M", (), {"UploadPostClient": lambda api_key=None: _FakeClient()}),
        )

        ppid, url = await up.publish(
            platform="upload_post_tiktok",
            file_path=str(video),
            script_id="s1",
            brand_id="drive_brand",
            attempts=1,
        )
        assert up.is_webhook_pending(ppid)
        assert up.extract_request_id(ppid) == "r-1"
        assert url is None
        assert upload_called["n"] == 1
