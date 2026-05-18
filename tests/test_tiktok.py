"""TikTok OAuth flow, publisher, and crypto helpers."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

# Minimal env so Settings() instantiates without a real .env.
os.environ.setdefault("DISPATCH_MODE", "dry_run")
os.environ.setdefault("SIGNAL_DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_SIGNAL", "0:test")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_API_KEY", "test")

# A valid Fernet key for the test suite — never used in production.
_TEST_FERNET_KEY = "l3mgT3MDKZ2g8oh2l8r4e1XaS0o7Q8mT9H5V1v3P2Hk="
os.environ.setdefault("AUTH_ENCRYPTION_KEY", _TEST_FERNET_KEY)
os.environ.setdefault("TIKTOK_CLIENT_KEY", "test_client_key")
os.environ.setdefault("TIKTOK_CLIENT_SECRET", "test_client_secret")


@pytest.fixture(autouse=True)
def _reset_registry_and_cache():
    """Clear caches before and after each test so env changes apply."""
    from social_signal import config as cfg
    from social_signal import crypto as cr

    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    cr._fernet.cache_clear()
    yield
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    cr._fernet.cache_clear()


# ---------------------------------------------------------------------------
# crypto: Fernet round-trip + HMAC state tokens
# ---------------------------------------------------------------------------

class TestCrypto:
    def test_encrypt_decrypt_round_trip(self):
        from social_signal import crypto
        secret = "tiktok-refresh-token-xyz"
        ct = crypto.encrypt(secret)
        assert ct != secret
        assert crypto.decrypt(ct) == secret

    def test_state_token_round_trip(self):
        from social_signal import crypto
        token = crypto.make_state_token({"b": "drive_brand", "p": "tiktok"})
        payload = crypto.verify_state_token(token)
        assert payload == {"b": "drive_brand", "p": "tiktok"}

    def test_state_token_tampered_signature_rejected(self):
        from social_signal import crypto
        token = crypto.make_state_token({"b": "drive_brand", "p": "tiktok"})
        tampered = token[:-4] + "AAAA"
        with pytest.raises(ValueError, match="signature"):
            crypto.verify_state_token(tampered)

    def test_state_token_expiry_rejected(self):
        from social_signal import crypto
        token = crypto.make_state_token({"b": "drive_brand"}, ttl_s=-1)
        with pytest.raises(ValueError, match="expired"):
            crypto.verify_state_token(token)


# ---------------------------------------------------------------------------
# OAuth flow helpers
# ---------------------------------------------------------------------------

class TestTikTokOAuth:
    def test_build_authorize_url_has_expected_params(self):
        from social_signal.oauth.tiktok import build_authorize_url

        url = build_authorize_url("drive_brand")
        assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
        assert "client_key=test_client_key" in url
        assert "response_type=code" in url
        assert "scope=user.info.basic%2Cvideo.upload%2Cvideo.publish" in url
        assert "redirect_uri=https%3A%2F%2Fgrow.example.com%2Foauth%2Ftiktok%2Fcallback" in url
        assert "state=" in url

    def test_parse_state_accepts_tiktok_platform(self):
        from social_signal.crypto import make_state_token
        from social_signal.oauth.tiktok import parse_state

        token = make_state_token({"b": "drive_brand", "p": "tiktok"})
        assert parse_state(token) == "drive_brand"

    def test_parse_state_rejects_wrong_platform(self):
        from social_signal.crypto import make_state_token
        from social_signal.oauth.tiktok import parse_state

        token = make_state_token({"b": "drive_brand", "p": "facebook"})
        with pytest.raises(ValueError, match="platform mismatch"):
            parse_state(token)


# ---------------------------------------------------------------------------
# TikTok publisher
# ---------------------------------------------------------------------------

class TestPlanChunks:
    """Chunk planner must satisfy TikTok's per-chunk size rules.

    Rules (Content Posting API, 2026-04):
      - Files ≤ 64 MB → single chunk of file_size bytes.
      - Multi-chunk: every chunk except the last equals chunk_size,
        and every chunk must be in [5 MB, 64 MB].
    """
    MB = 1024 * 1024

    def test_single_chunk_under_64mb(self):
        from social_signal.platforms.tiktok import _plan_chunks
        cs, n = _plan_chunks(10 * self.MB)
        assert n == 1
        assert cs == 10 * self.MB

    def test_single_chunk_exactly_64mb(self):
        from social_signal.platforms.tiktok import _plan_chunks
        cs, n = _plan_chunks(64 * self.MB)
        assert n == 1
        assert cs == 64 * self.MB

    def test_82mb_uses_8_chunks_with_larger_final(self):
        # The real-world bug: 82.7 MB / 10 MB chunks yielded 9 chunks with
        # a 2.66 MB final chunk (< 5 MB floor). Correct plan: 8 chunks,
        # chunk_size 10 MB, final chunk = file_size - 7*10 MB = 12.66 MB.
        from social_signal.platforms.tiktok import _plan_chunks
        file_size = 86683219   # 82.7 MB — an 82 MB file
        cs, n = _plan_chunks(file_size)
        assert n == 8
        assert cs == 10 * self.MB
        final = file_size - cs * (n - 1)
        assert 5 * self.MB <= final <= 64 * self.MB

    def test_final_chunk_always_between_5_and_64_mb(self):
        # Exhaustive sanity: any file size between 64 MB (exclusive) and
        # 640 MB (past any realistic video) must plan to a valid final chunk.
        from social_signal.platforms.tiktok import _plan_chunks
        for fs_mb in range(65, 640, 3):
            fs = fs_mb * self.MB
            cs, n = _plan_chunks(fs)
            if n == 1:
                assert cs == fs
            else:
                final = fs - cs * (n - 1)
                assert 5 * self.MB <= final <= 64 * self.MB, (
                    f"file_size={fs_mb}MB plan=({cs//self.MB}MB × {n}) "
                    f"→ final={final/self.MB:.2f}MB out of range"
                )


class TestTikTokPublisher:
    @pytest.mark.asyncio
    async def test_dry_run_returns_fake_id_and_no_http(self):
        from social_signal.platforms import tiktok

        # DISPATCH_MODE=dry_run from module env setup above.
        publish_id, url = await tiktok.publish(
            file_path="/does/not/exist.mp4",
            script_id="whatever",
            brand_id="drive_brand",
        )
        assert publish_id.startswith("tiktok-dry-")
        assert url is None

    @pytest.mark.asyncio
    async def test_live_mode_requires_brand_id(self, monkeypatch):
        from social_signal import config
        from social_signal.platforms import tiktok

        monkeypatch.setenv("DISPATCH_MODE", "live")
        config.settings.cache_clear()

        with pytest.raises(ValueError, match="brand_id is required"):
            await tiktok.publish(
                file_path="/tmp/nope.mp4",
                script_id="s",
                brand_id=None,
            )


# ---------------------------------------------------------------------------
# Token exchange + persistence (all HTTP mocked)
# ---------------------------------------------------------------------------

class TestTokenExchangeAndPersist:
    @pytest.mark.asyncio
    async def test_exchange_code_happy_path(self):
        from social_signal.oauth import tiktok as oauth

        fake_resp = _FakeResponse(
            200,
            {
                "access_token": "AT_123",
                "refresh_token": "RT_123",
                "expires_in": 86400,
                "scope": "user.info.basic,video.publish",
                "open_id": "open_abc",
            },
        )

        with patch("social_signal.oauth.tiktok.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=fake_resp)
            tokens = await oauth.exchange_code_for_tokens("dummy_code")

        assert tokens["access_token"] == "AT_123"
        assert tokens["open_id"] == "open_abc"

    @pytest.mark.asyncio
    async def test_exchange_code_raises_on_provider_error(self):
        from social_signal.oauth import tiktok as oauth

        fake_resp = _FakeResponse(400, {"error": "invalid_grant"})

        with patch("social_signal.oauth.tiktok.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=fake_resp)
            with pytest.raises(RuntimeError, match="token exchange failed"):
                await oauth.exchange_code_for_tokens("dummy_code")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal httpx.Response stand-in for mocked async calls."""
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload
