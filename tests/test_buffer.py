"""Buffer publisher — dry-run, routing, brand-config resolution, poll logic."""
from __future__ import annotations

import json
import os
import pathlib

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


def _write_brand(
    configs_dir: pathlib.Path,
    brand_id: str,
    *,
    channel_id: str | None = "buffer-channel-xyz",
    organization_id: str | None = "buffer-org-abc",
    enabled: bool = True,
) -> None:
    cfg: dict = {
        "brand_id": brand_id,
        "display_name": brand_id,
        "timezone": "UTC",
        "platforms": {},
    }
    block: dict = {"enabled": enabled}
    if channel_id is not None:
        block["channel_id"] = channel_id
    if organization_id is not None:
        block["organization_id"] = organization_id
    cfg["platforms"]["buffer_tiktok"] = block
    (configs_dir / f"{brand_id}.json").write_text(json.dumps(cfg))


class TestBufferPlatformMap:
    def test_known_platform_keys(self):
        from glitch_signal.platforms.buffer import _PLATFORM_MAP
        assert _PLATFORM_MAP["buffer_tiktok"] == "tiktok"
        assert _PLATFORM_MAP["buffer_instagram"] == "instagram"
        assert _PLATFORM_MAP["buffer_youtube"] == "youtube"


class TestBufferSentinel:
    def test_webhook_pending_roundtrip(self):
        from glitch_signal.platforms import buffer
        token = f"webhook_pending:{'x' * 24}"
        assert buffer.is_webhook_pending(token)
        assert buffer.extract_post_id(token) == "x" * 24

    def test_non_pending_tokens_rejected(self):
        from glitch_signal.platforms import buffer
        assert not buffer.is_webhook_pending("")
        assert not buffer.is_webhook_pending("plain-id")
        with pytest.raises(ValueError):
            buffer.extract_post_id("plain-id")


class TestBufferDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_fake_id_no_http(self):
        from glitch_signal.platforms import buffer
        publish_id, url = await buffer.publish(
            platform="buffer_tiktok",
            file_path="/does/not/matter.mp4",
            script_id="s1",
            brand_id="brand_dry",
        )
        assert publish_id.startswith("buffer-dry-")
        assert url is None


class TestBufferLiveValidation:
    """Live-path guards before any HTTP is attempted."""

    @pytest.mark.asyncio
    async def test_rejects_missing_brand_id(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        config.settings.cache_clear()
        with pytest.raises(ValueError, match="brand_id is required"):
            await buffer.publish(
                platform="buffer_tiktok", file_path="/x.mp4",
                script_id="s", brand_id=None,
            )

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "")
        config.settings.cache_clear()
        with pytest.raises(RuntimeError, match="BUFFER_API_TOKEN"):
            await buffer.publish(
                platform="buffer_tiktok", file_path="/x.mp4",
                script_id="s", brand_id="brand_t",
            )

    @pytest.mark.asyncio
    async def test_rejects_unknown_platform_key(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        config.settings.cache_clear()
        with pytest.raises(ValueError, match="unknown platform key"):
            await buffer.publish(
                platform="buffer_bogus", file_path="/x.mp4",
                script_id="s", brand_id="brand_t",
            )

    @pytest.mark.asyncio
    async def test_non_tiktok_targets_not_implemented(self, monkeypatch, tmp_path):
        """Module is TikTok-only today — non-tiktok targets must raise early,
        before any HTTP traffic."""
        from glitch_signal import config
        from glitch_signal.platforms import buffer

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_ig")
        # Overwrite the config to use buffer_instagram instead.
        (configs / "brand_ig.json").write_text(json.dumps({
            "brand_id": "brand_ig", "display_name": "brand_ig", "timezone": "UTC",
            "platforms": {"buffer_instagram": {
                "enabled": True, "channel_id": "c", "organization_id": "o",
            }},
        }))
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_ig")
        config.settings.cache_clear()
        config._reset_brand_registry_for_tests()

        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"x")
        with pytest.raises(NotImplementedError, match="only tiktok"):
            await buffer.publish(
                platform="buffer_instagram", file_path=str(vid),
                script_id="s", brand_id="brand_ig",
            )

    @pytest.mark.asyncio
    async def test_requires_channel_id(self, monkeypatch, tmp_path):
        from glitch_signal import config
        from glitch_signal.platforms import buffer

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_noc", channel_id="")
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_noc")
        config.settings.cache_clear()
        config._reset_brand_registry_for_tests()

        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"x")
        with pytest.raises(RuntimeError, match="channel_id"):
            await buffer.publish(
                platform="buffer_tiktok", file_path=str(vid),
                script_id="s", brand_id="brand_noc",
            )

    @pytest.mark.asyncio
    async def test_requires_organization_id(self, monkeypatch, tmp_path):
        from glitch_signal import config
        from glitch_signal.platforms import buffer

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_noo", organization_id="")
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_noo")
        config.settings.cache_clear()
        config._reset_brand_registry_for_tests()

        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"x")
        with pytest.raises(RuntimeError, match="organization_id"):
            await buffer.publish(
                platform="buffer_tiktok", file_path=str(vid),
                script_id="s", brand_id="brand_noo",
            )

    @pytest.mark.asyncio
    async def test_missing_video_file_raises(self, monkeypatch, tmp_path):
        from glitch_signal import config
        from glitch_signal.platforms import buffer

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_nf")
        monkeypatch.setenv("DISPATCH_MODE", "live")
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_nf")
        monkeypatch.setenv("MEDIA_PUBLIC_BASE_URL", "https://example.test")
        config.settings.cache_clear()
        config._reset_brand_registry_for_tests()

        with pytest.raises(FileNotFoundError):
            await buffer.publish(
                platform="buffer_tiktok",
                file_path=str(tmp_path / "missing.mp4"),
                script_id="s", brand_id="brand_nf",
            )


class TestBufferPollStatus:
    """Unit-test the poll parser by stubbing httpx. No network."""

    @pytest.mark.asyncio
    async def test_sent_returns_external_link(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        config.settings.cache_clear()

        class _FakeResp:
            def __init__(self, body): self._b = body
            def raise_for_status(self): pass
            def json(self): return self._b

        class _FakeClient:
            def __init__(self, body): self._b = body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return _FakeResp(self._b)

        body = {"data": {"post": {
            "id": "buf-1", "status": "sent",
            "externalLink": "https://www.tiktok.com/@user/video/123",
            "channelService": "tiktok",
        }}}
        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **kw: _FakeClient(body))

        ppid, url = await buffer.poll_status_for_post("buf-1", "org-1")
        assert ppid == "buf-1"
        assert url == "https://www.tiktok.com/@user/video/123"

    @pytest.mark.asyncio
    async def test_sending_returns_none_none(self, monkeypatch):
        """`sending`/processing → in-flight, reconcile should retry next tick."""
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        config.settings.cache_clear()

        class _FakeResp:
            def __init__(self, body): self._b = body
            def raise_for_status(self): pass
            def json(self): return self._b

        class _FakeClient:
            def __init__(self, body): self._b = body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return _FakeResp(self._b)

        body = {"data": {"post": {
            "id": "buf-1", "status": "sending", "externalLink": None,
            "channelService": "tiktok",
        }}}
        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **kw: _FakeClient(body))

        ppid, url = await buffer.poll_status_for_post("buf-1", "org-1")
        assert ppid is None and url is None

    @pytest.mark.asyncio
    async def test_failed_raises(self, monkeypatch):
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        config.settings.cache_clear()

        class _FakeResp:
            def __init__(self, body): self._b = body
            def raise_for_status(self): pass
            def json(self): return self._b

        class _FakeClient:
            def __init__(self, body): self._b = body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return _FakeResp(self._b)

        body = {"data": {"post": {
            "id": "buf-1", "status": "failed", "externalLink": None,
            "channelService": "tiktok",
        }}}
        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **kw: _FakeClient(body))

        with pytest.raises(RuntimeError, match="failed"):
            await buffer.poll_status_for_post("buf-1", "org-1")

    @pytest.mark.asyncio
    async def test_graphql_errors_raise(self, monkeypatch):
        """Rate limit / auth errors from Buffer bubble up so the reconcile
        loop leaves the row for the next tick."""
        from glitch_signal import config
        from glitch_signal.platforms import buffer
        monkeypatch.setenv("BUFFER_API_TOKEN", "t")
        config.settings.cache_clear()

        class _FakeResp:
            def __init__(self, body): self._b = body
            def raise_for_status(self): pass
            def json(self): return self._b

        class _FakeClient:
            def __init__(self, body): self._b = body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return _FakeResp(self._b)

        body = {"errors": [{
            "message": "Too many requests",
            "extensions": {"code": "RATE_LIMIT_EXCEEDED", "window": "24h"},
        }]}
        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **kw: _FakeClient(body))

        with pytest.raises(RuntimeError, match="RATE_LIMIT_EXCEEDED|Too many"):
            await buffer.poll_status_for_post("buf-1", "org-1")


class TestPublishPriority:
    """Priority list must prefer buffer_tiktok over upload_post_tiktok."""

    def test_tiktok_priority_order(self):
        from glitch_signal.config import _PUBLISH_PRIORITY
        tiktok_list = _PUBLISH_PRIORITY["tiktok"]
        assert tiktok_list[0] == "buffer_tiktok", (
            f"buffer_tiktok must be first for tiktok; got {tiktok_list}"
        )
        assert "upload_post_tiktok" in tiktok_list
