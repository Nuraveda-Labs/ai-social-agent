"""TikTok OAuth v2 flow + token refresh.

Flow: GET /oauth/tiktok/start?brand=<id>
         → signed state token → redirect to TikTok authorize URL
      GET /oauth/tiktok/callback?code=...&state=...
         → verify state → exchange code → store encrypted tokens

Docs: https://developers.tiktok.com/doc/login-kit-web
      https://developers.tiktok.com/doc/content-posting-api-get-started
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import structlog

from glitch_signal.config import settings
from glitch_signal.crypto import make_state_token, verify_state_token
from glitch_signal.oauth import storage

log = structlog.get_logger(__name__)

_TOKEN_EXCHANGE_PATH = "/v2/oauth/token/"
_AUTHORIZE_PATH = "/v2/auth/authorize/"


def build_authorize_url(brand_id: str, *, scopes: str | None = None) -> str:
    s = settings()
    if not s.tiktok_client_key:
        raise RuntimeError(
            "TIKTOK_CLIENT_KEY is not set — cannot build TikTok authorize URL."
        )

    state = make_state_token({"b": brand_id, "p": "tiktok"})
    params = {
        "client_key": s.tiktok_client_key,
        "response_type": "code",
        "scope": scopes or s.tiktok_default_scopes,
        "redirect_uri": s.tiktok_redirect_uri,
        "state": state,
    }
    return f"{s.tiktok_auth_base}{_AUTHORIZE_PATH}?{urlencode(params)}"


def parse_state(state: str) -> str:
    """Return brand_id from a verified state token. Raises ValueError on bad state."""
    payload = verify_state_token(state)
    if payload.get("p") != "tiktok":
        raise ValueError("state token platform mismatch")
    brand_id = payload.get("b")
    if not brand_id:
        raise ValueError("state token missing brand")
    return brand_id


async def exchange_code_for_tokens(code: str) -> dict:
    """Exchange authorization code for access + refresh tokens.

    Returns the raw provider response (dict). Caller is responsible for
    persisting it via oauth.storage.upsert.
    """
    s = settings()
    if not s.tiktok_client_key or not s.tiktok_client_secret:
        raise RuntimeError("TikTok client credentials not configured")

    data = {
        "client_key": s.tiktok_client_key,
        "client_secret": s.tiktok_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": s.tiktok_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{s.tiktok_api_base}{_TOKEN_EXCHANGE_PATH}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    payload = _safe_json(resp)
    if resp.status_code >= 400 or "access_token" not in payload:
        log.error("tiktok.token_exchange_failed", status=resp.status_code, body=payload)
        raise RuntimeError(f"TikTok token exchange failed: {payload}")
    return payload


async def refresh_access_token(refresh_token: str) -> dict:
    s = settings()
    data = {
        "client_key": s.tiktok_client_key,
        "client_secret": s.tiktok_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{s.tiktok_api_base}{_TOKEN_EXCHANGE_PATH}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    payload = _safe_json(resp)
    if resp.status_code >= 400 or "access_token" not in payload:
        log.error("tiktok.token_refresh_failed", status=resp.status_code, body=payload)
        raise RuntimeError(f"TikTok token refresh failed: {payload}")
    return payload


async def persist_tokens(brand_id: str, tokens: dict) -> str:
    """Write the token response to platform_auth. Returns the row id."""
    expires_in = int(tokens.get("expires_in") or 0)
    expires_at = (
        datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=expires_in)
        if expires_in
        else None
    )
    scope_raw = tokens.get("scope") or ""
    scopes = [s.strip() for s in str(scope_raw).split(",") if s.strip()]

    return await storage.upsert(
        brand_id=brand_id,
        platform="tiktok",
        account_identifier=tokens.get("open_id"),
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        access_token_expires_at=expires_at,
        scopes=scopes,
        raw_provider_response=tokens,
    )


async def get_fresh_access_token(brand_id: str) -> str:
    """Return a currently-valid access token, refreshing if needed.

    Raises RuntimeError if no tokens exist or refresh fails.
    """
    auth = await storage.get(brand_id, "tiktok")
    if not auth:
        raise RuntimeError(
            f"No TikTok auth for brand={brand_id}. Run the OAuth flow at "
            f"{settings().public_base_url}/oauth/tiktok/start?brand={brand_id}"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    # Refresh ~10 min before expiry to avoid race with in-flight requests.
    if auth.access_token_expires_at and auth.access_token_expires_at - timedelta(minutes=10) <= now:
        if not auth.refresh_token:
            await storage.mark_needs_reauth(brand_id, "tiktok")
            raise RuntimeError("TikTok access token expired and no refresh token available")

        try:
            refreshed = await refresh_access_token(auth.refresh_token)
        except Exception:
            await storage.mark_needs_reauth(brand_id, "tiktok")
            raise
        await persist_tokens(brand_id, refreshed)
        return refreshed["access_token"]

    return auth.access_token


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": "non_json_response", "body": resp.text[:500]}
