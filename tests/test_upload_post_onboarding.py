"""Upload-Post JWT onboarding — URL generation + response-shape tolerance."""
from __future__ import annotations

import os
import sys

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


def _install_fake_upload_post(monkeypatch, resp):
    """Inject a fake upload_post module whose generate_jwt returns `resp`."""
    captured: dict = {}

    class _C:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

        def generate_jwt(self, **kwargs):
            captured.update(kwargs)
            return resp

    monkeypatch.setitem(
        sys.modules,
        "upload_post",
        type("M", (), {"UploadPostClient": _C}),
    )
    return captured


class TestUrlExtraction:
    @pytest.mark.asyncio
    async def test_flat_access_url(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        captured = _install_fake_upload_post(
            monkeypatch, {"access_url": "https://app.upload-post.com/connect/abc"}
        )

        url = await generate_onboarding_url(username="NewBrand", platforms=["tiktok"])
        assert url == "https://app.upload-post.com/connect/abc"
        assert captured["username"] == "NewBrand"
        assert captured["platforms"] == ["tiktok"]

    @pytest.mark.asyncio
    async def test_flat_url_key(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        _install_fake_upload_post(
            monkeypatch, {"url": "https://app.upload-post.com/connect/xyz"}
        )

        url = await generate_onboarding_url(username="NewBrand")
        assert url.endswith("/connect/xyz")

    @pytest.mark.asyncio
    async def test_nested_data_shape(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        _install_fake_upload_post(
            monkeypatch,
            {"data": {"access_url": "https://app.upload-post.com/connect/nested"}},
        )

        url = await generate_onboarding_url(username="NewBrand")
        assert url.endswith("/connect/nested")

    @pytest.mark.asyncio
    async def test_no_url_in_response_raises(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        _install_fake_upload_post(monkeypatch, {"ok": True, "msg": "pending"})

        with pytest.raises(RuntimeError, match="did not return a usable URL"):
            await generate_onboarding_url(username="NewBrand")

    @pytest.mark.asyncio
    async def test_non_http_url_rejected(self, monkeypatch):
        """A token-only string (no scheme) would let the client open a bad
        thing if we handed it back — reject anything that doesn't look like
        an http(s) URL."""
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        _install_fake_upload_post(monkeypatch, {"url": "javascript:alert(1)"})

        with pytest.raises(RuntimeError):
            await generate_onboarding_url(username="NewBrand")


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_username_raises(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()

        with pytest.raises(ValueError, match="username is required"):
            await generate_onboarding_url(username="")

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "")
        cfg.settings.cache_clear()

        with pytest.raises(RuntimeError, match="UPLOAD_POST_API_KEY"):
            await generate_onboarding_url(username="NewBrand")

    @pytest.mark.asyncio
    async def test_unsupported_platform_raises(self, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()

        with pytest.raises(ValueError, match="unsupported platforms"):
            await generate_onboarding_url(
                username="NewBrand", platforms=["myspace"]
            )


class TestOptionalKwargsPropagate:
    @pytest.mark.asyncio
    async def test_only_set_kwargs_reach_sdk(self, monkeypatch):
        """Nones must be stripped so they don't override Upload-Post defaults
        and the SDK receives a clean minimal kwargs dict."""
        from glitch_signal import config as cfg
        from glitch_signal.onboarding.upload_post import generate_onboarding_url

        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        cfg.settings.cache_clear()
        captured = _install_fake_upload_post(
            monkeypatch, {"access_url": "https://x/y"}
        )

        await generate_onboarding_url(
            username="NewBrand",
            platforms=["tiktok"],
            redirect_url="https://brand.example.com/done",
        )
        # Present — set by caller.
        assert captured["username"] == "NewBrand"
        assert captured["platforms"] == ["tiktok"]
        assert captured["redirect_url"] == "https://brand.example.com/done"
        # Absent — we did not pass these.
        assert "logo_image" not in captured
        assert "show_calendar" not in captured
        assert "connect_title" not in captured
