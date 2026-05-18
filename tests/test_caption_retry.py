"""Gemini transient-error retry — _acompletion_with_retry behaviour."""
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


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Skip the real 30s backoff so tests finish quickly."""
    import asyncio

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def _make_response(body: str):
    class _M:
        content = body
    class _C:
        message = _M()
    class _R:
        choices = [_C()]
    return _R()


class TestRetryOnTransient:
    @pytest.mark.asyncio
    async def test_service_unavailable_then_success(self, monkeypatch):
        import litellm

        from glitch_signal.agent.nodes.caption_writer import _acompletion_with_retry

        calls = {"n": 0}

        async def fake(**kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise litellm.ServiceUnavailableError(
                    "overloaded", model="gemini/gemini-2.5-flash", llm_provider="gemini"
                )
            return _make_response('{"ok": true}')

        monkeypatch.setattr(
            "glitch_signal.agent.nodes.caption_writer.litellm.acompletion", fake
        )
        resp = await _acompletion_with_retry(model="x", messages=[])
        assert resp.choices[0].message.content == '{"ok": true}'
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_rate_limit_retried(self, monkeypatch):
        import litellm

        from glitch_signal.agent.nodes.caption_writer import _acompletion_with_retry

        calls = {"n": 0}

        async def fake(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise litellm.RateLimitError(
                    "throttled", model="gemini/gemini-2.5-flash", llm_provider="gemini"
                )
            return _make_response("{}")

        monkeypatch.setattr(
            "glitch_signal.agent.nodes.caption_writer.litellm.acompletion", fake
        )
        await _acompletion_with_retry(model="x", messages=[])
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_exhausts_attempts_then_reraises(self, monkeypatch):
        """After 5 transient failures, the final exception bubbles."""
        import litellm

        from glitch_signal.agent.nodes.caption_writer import _acompletion_with_retry

        calls = {"n": 0}

        async def fake(**kwargs):
            calls["n"] += 1
            raise litellm.ServiceUnavailableError(
                "still bad", model="x", llm_provider="gemini"
            )

        monkeypatch.setattr(
            "glitch_signal.agent.nodes.caption_writer.litellm.acompletion", fake
        )
        with pytest.raises(litellm.ServiceUnavailableError):
            await _acompletion_with_retry(model="x", messages=[])
        assert calls["n"] == 5   # max_attempts

    @pytest.mark.asyncio
    async def test_hard_error_does_not_retry(self, monkeypatch):
        """AuthError / BadRequest should propagate immediately — no retry."""
        import litellm

        from glitch_signal.agent.nodes.caption_writer import _acompletion_with_retry

        calls = {"n": 0}

        async def fake(**kwargs):
            calls["n"] += 1
            raise litellm.AuthenticationError(
                "bad key", model="x", llm_provider="gemini"
            )

        monkeypatch.setattr(
            "glitch_signal.agent.nodes.caption_writer.litellm.acompletion", fake
        )
        with pytest.raises(litellm.AuthenticationError):
            await _acompletion_with_retry(model="x", messages=[])
        assert calls["n"] == 1   # no retry on hard error

    @pytest.mark.asyncio
    async def test_first_attempt_success_no_retry(self, monkeypatch):
        from glitch_signal.agent.nodes.caption_writer import _acompletion_with_retry

        calls = {"n": 0}

        async def fake(**kwargs):
            calls["n"] += 1
            return _make_response("{}")

        monkeypatch.setattr(
            "glitch_signal.agent.nodes.caption_writer.litellm.acompletion", fake
        )
        await _acompletion_with_retry(model="x", messages=[])
        assert calls["n"] == 1
