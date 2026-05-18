"""Zernio publisher — multi-platform social posting via an audited partner app.

Zernio (formerly Late) is a managed social-posting API that holds audited
apps for TikTok, IG, FB, YT, X, etc. Using Zernio as a thin publish layer
sidesteps the TikTok-side audit wall that blocks direct-post for unaudited
first-party apps.

Upload strategy:
  Zernio's own direct-upload endpoint is a Vercel serverless function with
  a ~4.5 MB payload cap, and their upload_large() path requires a caller-
  provided Vercel Blob token. BUT posts.create() accepts an external URL
  directly in media_items — Zernio fetches from that URL server-side. So
  we host the video ourselves via nginx on grow.example.com and
  hand Zernio an HMAC-signed public URL to it. Skips the 4 MB cap, no
  Vercel dependency, no extra vendor.

Security model for the public URL:
  - Path carries a per-file HMAC token signed with AUTH_ENCRYPTION_KEY
    (already present for Fernet)
  - URL expires after MEDIA_URL_TTL_S (default 1 hour) — enough time for
    Zernio to fetch, not long enough to be a long-term leak
  - The /media/fetch endpoint verifies the HMAC before streaming bytes

Per-brand config lives under platforms.zernio_tiktok (or zernio_<platform>)
and must carry:
  - enabled: true
  - account_id: Zernio internal id from client.accounts.list()

The platform string on ScheduledPost rows is "zernio_tiktok" so the
dispatcher in agent/nodes/publisher.py can route without disturbing the
direct "tiktok" path.

All live calls gated behind DISPATCH_MODE=dry_run.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid

import structlog

from social_signal.config import brand_config, settings
from social_signal.crypto import make_state_token
from social_signal.db.models import ContentScript
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

# Map of our platform-key suffixes to Zernio's platform enum value.
_PLATFORM_MAP = {
    "zernio_tiktok":    "tiktok",
    "zernio_instagram": "instagram",
    "zernio_youtube":   "youtube",
    "zernio_twitter":   "twitter",
    "zernio_facebook":  "facebook",
}

# HMAC token TTL for the public media URL we hand Zernio.
_MEDIA_URL_TTL_S = 60 * 60   # 1 hour — well beyond any realistic fetch window


async def publish(
    platform: str,
    file_path: str,
    script_id: str,
    brand_id: str | None = None,
) -> tuple[str, str | None]:
    """Publish a video via Zernio. Returns (zernio_post_id, share_url|None)."""
    s = settings()

    if s.is_dry_run:
        fake_id = f"zernio-dry-{uuid.uuid4().hex[:10]}"
        log.info(
            "zernio.publish.dry_run",
            publish_id=fake_id,
            file_path=file_path,
            brand_id=brand_id,
            platform=platform,
        )
        return fake_id, None

    if not brand_id:
        raise ValueError("zernio.publish: brand_id is required for live publish")
    if not s.zernio_api_key:
        raise RuntimeError("ZERNIO_API_KEY is not set")

    target = _PLATFORM_MAP.get(platform)
    if not target:
        raise ValueError(f"zernio.publish: unknown platform key {platform!r}")

    cfg_block = (
        brand_config(brand_id).get("platforms", {}).get(platform, {}) or {}
    )
    account_id = cfg_block.get("account_id")
    if not account_id:
        raise RuntimeError(
            f"zernio.publish: brand={brand_id!r} is missing "
            f"platforms.{platform}.account_id — find it via "
            f"client.accounts.list() and put it in the brand config"
        )

    path = pathlib.Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"zernio.publish: file missing: {file_path}")

    caption, title, hashtags = await _read_caption(script_id, brand_id, cfg_block)

    # Build a short-lived public URL. Zernio fetches from it server-side,
    # so we never touch Zernio's 4 MB direct-upload limit or their Vercel
    # Blob path.
    media_url = _build_signed_media_url(path)
    log.info(
        "zernio.publish.media_url_issued",
        brand_id=brand_id,
        file_path=str(path),
        media_url_host=media_url.split("/")[2] if "://" in media_url else media_url,
    )

    # Zernio SDK is blocking httpx-under-the-hood → run in a thread so we
    # don't block the scheduler's event loop.
    return await asyncio.to_thread(
        _publish_sync,
        api_key=s.zernio_api_key,
        target_platform=target,
        account_id=account_id,
        media_url=media_url,
        caption=caption,
        title=title,
        hashtags=hashtags,
        tiktok_settings=_tiktok_settings(cfg_block) if target == "tiktok" else None,
    )


# ---------------------------------------------------------------------------
# Blocking worker — runs in a thread via asyncio.to_thread
# ---------------------------------------------------------------------------

def _publish_sync(
    *,
    api_key: str,
    target_platform: str,
    account_id: str,
    media_url: str,
    caption: str,
    title: str,
    hashtags: list[str],
    tiktok_settings: dict | None,
) -> tuple[str, str | None]:
    import zernio

    client = zernio.Zernio(api_key=api_key)

    # No upload step — Zernio fetches from media_url server-side.
    kwargs: dict = dict(
        content=caption,
        platforms=[{"platform": target_platform, "accountId": account_id}],
        media_items=[{"type": "video", "url": media_url}],
        publish_now=True,
        hashtags=hashtags or None,
    )
    if title:
        kwargs["title"] = title
    if tiktok_settings is not None:
        kwargs["tiktok_settings"] = tiktok_settings

    try:
        resp = client.posts.create(**kwargs)
    except zernio.ZernioAPIError as exc:
        # Retry-after-success recovery: a previous attempt published on
        # Zernio but our code raised before we could record the post_id.
        # On retry, Zernio's dedup surfaces as a 409. Look up the existing
        # post on the account and return it so the scheduler marks done
        # instead of retrying into a growing failure counter.
        if not _is_duplicate_error(exc):
            raise
        recovered = _recover_published_post(
            client,
            target_platform=target_platform,
            account_id=account_id,
            caption=caption,
        )
        if not recovered:
            log.error(
                "zernio.publish.duplicate_unrecoverable",
                target_platform=target_platform,
                status_code=getattr(exc, "status_code", None),
                error=str(exc)[:200],
            )
            raise
        post_id, share_url = recovered
        log.info(
            "zernio.publish.recovered_from_duplicate",
            target_platform=target_platform,
            zernio_post_id=post_id,
            share_url=share_url,
        )
        return post_id, share_url

    post = getattr(resp, "post", None)
    post_id = getattr(post, "id", None) or getattr(post, "field_id", None)
    share_url = getattr(post, "url", None) or getattr(post, "share_url", None)

    if not post_id:
        # Best-effort extraction from model_dump for SDK variants.
        dump = resp.model_dump() if hasattr(resp, "model_dump") else {}
        post_dict = dump.get("post") or {}
        post_id = post_dict.get("id") or post_dict.get("_id") or str(uuid.uuid4())
        share_url = share_url or post_dict.get("url")

    log.info(
        "zernio.publish.done",
        target_platform=target_platform,
        zernio_post_id=post_id,
        share_url=share_url,
    )
    return post_id, share_url


# ---------------------------------------------------------------------------
# Duplicate / 409 recovery
# ---------------------------------------------------------------------------

def _is_duplicate_error(exc: Exception) -> bool:
    """Zernio signals duplicates via status_code==409 or a 'duplicate'-shaped message."""
    code = getattr(exc, "status_code", None)
    if code == 409:
        return True
    msg = str(exc).lower()
    return "duplicate" in msg or "already" in msg


def _recover_published_post(
    client,
    *,
    target_platform: str,
    account_id: str,
    caption: str,
) -> tuple[str, str | None] | None:
    """List recent posts on the account and return the match for our caption.

    We filter by account (`profile_id`) and platform, then match on `content`
    equality. Only the live per-platform post_id/url is useful to the caller,
    so we dig into `Post.platforms[].platformPostId / publishedUrl`.
    """
    try:
        listing = client.posts.list(
            profile_id=account_id,
            platform=target_platform,
            limit=20,
        )
    except Exception as exc:
        log.warning(
            "zernio.publish.recover_list_failed",
            error=str(exc)[:200],
        )
        return None

    posts = getattr(listing, "posts", None) or []
    for p in posts:
        content = (getattr(p, "content", None) or "").strip()
        if content != caption.strip():
            continue
        plats = getattr(p, "platforms", None) or []
        for pl in plats:
            if getattr(pl, "platform", None) != target_platform:
                continue
            ppid = getattr(pl, "platformPostId", None)
            purl = getattr(pl, "publishedUrl", None)
            if ppid or purl:
                return ppid or getattr(p, "id", None) or str(uuid.uuid4()), purl
        # Fallback: platforms block missing post_id — return the Zernio post id
        zid = getattr(p, "id", None)
        if zid:
            return zid, None
    return None


# ---------------------------------------------------------------------------
# Signed public-media URL
# ---------------------------------------------------------------------------

def _build_signed_media_url(local_path: pathlib.Path) -> str:
    """Return an HMAC-signed public URL that the /media/fetch endpoint accepts.

    The token encodes the exact absolute path so a token issued for file A
    cannot be used to fetch file B. nginx proxies /media/* on
    grow.example.com to :3111/media/*, which verifies the token
    before streaming bytes.
    """
    s = settings()
    token = make_state_token(
        {"p": str(local_path.resolve()), "k": "media"},
        ttl_s=_MEDIA_URL_TTL_S,
    )
    base = s.media_public_base_url.rstrip("/")
    return f"{base}/media/fetch?token={token}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _read_caption(
    script_id: str, brand_id: str, cfg_block: dict
) -> tuple[str, str, list[str]]:
    """Pull the caption + title + hashtags out of the ContentScript row.

    Caption writer stores the full post text (including hashtags at the
    bottom) in ContentScript.script_body. We re-extract the hashtag list
    so the Zernio post carries them as structured fields too — some
    platforms render hashtags differently when they're separate vs
    embedded in the caption body.
    """
    factory = _session_factory()
    async with factory() as session:
        cs = await session.get(ContentScript, script_id) if script_id else None

    caption = (cs.script_body if cs else "").strip()
    title = ""
    if caption:
        first = caption.split(".")[0][:100].strip()
        title = first or caption[:100].strip()
    else:
        title = brand_config(brand_id).get("display_name", brand_id)

    hashtags: list[str] = []
    for tok in caption.split():
        if tok.startswith("#") and len(tok) > 1:
            hashtags.append(tok[1:].rstrip(".,!?").lower())
    if not hashtags:
        hashtags = [
            t.lstrip("#").strip().lower()
            for t in (cfg_block.get("default_tags") or [])
            if t
        ]

    return caption, title, hashtags


def _tiktok_settings(cfg_block: dict) -> dict:
    """Map our brand config's TikTok prefs into Zernio's tiktok_settings dict."""
    return {
        "privacyLevel": cfg_block.get("default_privacy_level", "PUBLIC_TO_EVERYONE"),
        "disableDuet": bool(cfg_block.get("disable_duet", False)),
        "disableStitch": bool(cfg_block.get("disable_stitch", False)),
        "disableComment": bool(cfg_block.get("disable_comment", False)),
        "videoCoverTimestampMs": int(cfg_block.get("video_cover_timestamp_ms", 1000)),
    }
