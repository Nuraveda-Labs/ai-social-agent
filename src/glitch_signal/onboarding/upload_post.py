"""Upload-Post JWT onboarding — 1-click brand account linking.

Upload-Post lets a client connect their own TikTok / Instagram / YouTube
/ LinkedIn / … account into OUR Upload-Post workspace without either
party touching the dashboard. We call `generate_jwt(username, ...)` and
get back a short-lived URL we hand the client; they open it, pick which
platforms to connect, and the tokens land in our account.

Typical flow:
  1. Create a new Upload-Post profile for the brand:
       client.create_user(username="NewBrand")
  2. Generate a connect URL for that profile:
       url = generate_onboarding_url(username="NewBrand", platforms=["tiktok"])
  3. Send the URL to the brand over Telegram / email / etc.
  4. Once they link, you'll get `social_account_connected` webhooks on
     /webhooks/upload_post — the brand config's
     platforms.upload_post_<target>.user gets set to the profile name and
     publishing starts working.

This module keeps the API surface tiny and easy to mock; the actual
HTTP call lives inside the Upload-Post SDK's `generate_jwt` method.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from glitch_signal.config import settings

log = structlog.get_logger(__name__)


# Upload-Post's supported `platforms` enum (for argv validation). Kept
# separately from platforms/upload_post.py::_PLATFORM_MAP so this module
# has no cross-module coupling.
SUPPORTED_PLATFORMS = frozenset({
    "tiktok", "instagram", "youtube", "linkedin", "facebook",
    "x", "threads", "pinterest", "bluesky", "reddit",
})


async def generate_onboarding_url(
    username: str,
    *,
    platforms: list[str] | None = None,
    redirect_url: str | None = None,
    logo_image: str | None = None,
    redirect_button_text: str | None = None,
    connect_title: str | None = None,
    connect_description: str | None = None,
    show_calendar: bool | None = None,
    readonly_calendar: bool | None = None,
) -> str:
    """Return a short-lived URL that links social accounts into `username`.

    Raises ValueError on bad platform names and RuntimeError if the API
    call succeeds but doesn't return a usable URL.
    """
    s = settings()
    if not s.upload_post_api_key:
        raise RuntimeError("UPLOAD_POST_API_KEY is not set")
    if not username:
        raise ValueError("generate_onboarding_url: username is required")

    if platforms is not None:
        bad = [p for p in platforms if p not in SUPPORTED_PLATFORMS]
        if bad:
            raise ValueError(
                f"generate_onboarding_url: unsupported platforms {bad}. "
                f"Supported: {sorted(SUPPORTED_PLATFORMS)}"
            )

    resp = await asyncio.to_thread(
        _call_generate_jwt,
        api_key=s.upload_post_api_key,
        username=username,
        platforms=platforms,
        redirect_url=redirect_url,
        logo_image=logo_image,
        redirect_button_text=redirect_button_text,
        connect_title=connect_title,
        connect_description=connect_description,
        show_calendar=show_calendar,
        readonly_calendar=readonly_calendar,
    )
    url = _extract_url(resp)
    if not url:
        raise RuntimeError(
            f"generate_onboarding_url: Upload-Post did not return a usable URL: "
            f"{str(resp)[:300]}"
        )
    log.info(
        "upload_post.onboarding.url_generated",
        username=username,
        platforms=platforms,
        url_host=url.split("/")[2] if "://" in url else url,
    )
    return url


def _call_generate_jwt(*, api_key: str, username: str, **kwargs: Any) -> dict:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    # Drop Nones so we don't override Upload-Post's defaults with nulls.
    clean = {k: v for k, v in kwargs.items() if v is not None}
    return client.generate_jwt(username=username, **clean)


def _extract_url(resp: Any) -> str | None:
    """Upload-Post has shipped 2-3 different response shapes for this. Probe."""
    if not isinstance(resp, dict):
        return None
    # Most common: top-level {"access_url": "..."} or {"url": "..."}.
    for key in ("access_url", "url", "jwt_url", "connect_url"):
        v = resp.get(key)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    # Nested: {"data": {"url": "..."}}.
    data = resp.get("data") or resp.get("result")
    if isinstance(data, dict):
        for key in ("access_url", "url", "jwt_url", "connect_url"):
            v = data.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
    return None
