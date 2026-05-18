"""Upload-Post publisher — multi-platform social posting via an audited partner app.

Third publisher in the repo, alongside:
  - platforms/tiktok.py  (direct-post to TikTok, blocked for unaudited apps)
  - platforms/zernio.py  (another audited vendor)

Why Upload-Post in addition to Zernio:
  - $16/mo (annual) Basic tier gives unlimited uploads across 5 profiles — cheaper
    than Zernio for real volume
  - Richer API surface: comments read+reply, per-platform analytics,
    scheduled-post management, webhooks on publish success/failure
  - Accepts both local paths AND URLs on upload_video → we reuse the
    signed /media/fetch URL pattern built for Zernio. Zero re-upload.

Platform-key convention mirrors zernio_*:
  upload_post_tiktok, upload_post_instagram, upload_post_youtube, …

Per-brand config lives under platforms.upload_post_<target> and must carry:
  - enabled: true
  - user: <Upload-Post profile username, e.g. "MyBrand">

Webhook-driven finalization:
  upload_video() returns immediately with a request_id. Upload-Post then
  transcodes + publishes asynchronously and POSTs the `upload_completed`
  event to /webhooks/upload_post/<secret>. The publisher returns a
  sentinel `webhook_pending:<request_id>` so publisher.py knows to set
  scheduled_post.status = "awaiting_webhook" instead of writing a
  PublishedPost row immediately — that row is written by the webhook
  handler when the real platform_post_id arrives.

  Fallback: if no webhook lands within UPLOAD_POST_WEBHOOK_RECONCILE_AFTER_S,
  the scheduler polls get_status(request_id) to finalize the row. This
  protects against dropped webhooks / our server being down during the
  callback.

DISPATCH_MODE=dry_run short-circuits without calling the SDK.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid

import structlog

from glitch_signal.config import brand_config, settings
from glitch_signal.crypto import make_state_token
from glitch_signal.db.models import ContentScript
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

# Map our platform-key suffixes to Upload-Post's platform enum values.
# Upload-Post's canonical platform names (from their SDK):
#   tiktok, instagram, youtube, linkedin, facebook, pinterest, threads,
#   bluesky, x, reddit, google_business
# Platforms where Upload-Post's SDK `title` kwarg IS the caption body
# (not a separate title field). See the field-mapping comment in
# _submit_upload for the full explanation.
_TITLE_IS_CAPTION = ("tiktok", "instagram", "x", "threads", "bluesky")

_PLATFORM_MAP = {
    "upload_post_tiktok":    "tiktok",
    "upload_post_instagram": "instagram",
    "upload_post_youtube":   "youtube",
    "upload_post_linkedin":  "linkedin",
    "upload_post_facebook":  "facebook",
    "upload_post_x":         "x",
    "upload_post_threads":   "threads",
    "upload_post_pinterest": "pinterest",
    "upload_post_bluesky":   "bluesky",
    "upload_post_reddit":    "reddit",
}

# TTL for the signed URL we hand Upload-Post. Their upload worker usually
# fetches within seconds; 1 hour is a very generous ceiling.
_MEDIA_URL_TTL_S = 60 * 60

# Sentinel returned by publish() when the vendor accepted the upload and is
# now processing asynchronously. The scheduler should mark the ScheduledPost
# as awaiting_webhook and wait for the webhook callback (or reconciliation
# sweep) to finalize.
_WEBHOOK_PENDING_PREFIX = "webhook_pending:"


def is_webhook_pending(token: str) -> bool:
    """True if publish() returned a pending sentinel rather than a finalized id."""
    return isinstance(token, str) and token.startswith(_WEBHOOK_PENDING_PREFIX)


def extract_request_id(token: str) -> str:
    """Return the Upload-Post request_id from a pending sentinel."""
    if not is_webhook_pending(token):
        raise ValueError(f"Not a webhook-pending sentinel: {token!r}")
    return token[len(_WEBHOOK_PENDING_PREFIX):]


async def publish(
    platform: str,
    file_path: str | None,
    script_id: str,
    brand_id: str | None = None,
    attempts: int = 1,
) -> tuple[str, str | None]:
    """Publish content via Upload-Post. Returns (provider_post_id, share_url|None).

    Routes to the right Upload-Post SDK method based on `content_type` in the
    brand platform config block:
      - "video"    (default) → upload_video
      - "text"               → upload_text   (LinkedIn, X, Threads, …)
      - "image"              → upload_photos (LinkedIn, Instagram, …)
      - "document"           → upload_document (LinkedIn PDF carousel)

    `attempts` is the scheduler's attempt counter for this ScheduledPost (1 on
    the first call, ≥2 on retries). Unlike Zernio, Upload-Post does NOT dedup
    server-side — a naive retry would double-post. So on `attempts > 1` we
    peek at get_history first and short-circuit if we find a recent matching
    upload, which recovers from the retry-after-success scenario without ever
    asking Upload-Post to publish again.
    """
    s = settings()

    if s.is_dry_run:
        fake_id = f"uploadpost-dry-{uuid.uuid4().hex[:10]}"
        log.info(
            "upload_post.publish.dry_run",
            publish_id=fake_id,
            file_path=file_path,
            brand_id=brand_id,
            platform=platform,
        )
        return fake_id, None

    if not brand_id:
        raise ValueError("upload_post.publish: brand_id is required for live publish")
    if not s.upload_post_api_key:
        raise RuntimeError("UPLOAD_POST_API_KEY is not set")

    target = _PLATFORM_MAP.get(platform)
    if not target:
        raise ValueError(f"upload_post.publish: unknown platform key {platform!r}")

    cfg_block = (
        brand_config(brand_id).get("platforms", {}).get(platform, {}) or {}
    )
    user = cfg_block.get("user")
    if not user:
        raise RuntimeError(
            f"upload_post.publish: brand={brand_id!r} missing "
            f"platforms.{platform}.user — the Upload-Post managed-user profile name"
        )

    content_type = cfg_block.get("content_type", "video")
    caption, title, _hashtags = await _read_caption(script_id, brand_id, cfg_block)

    # ── Text post ────────────────────────────────────────────────────────────
    if content_type == "text":
        request_id = await asyncio.to_thread(
            _submit_text,
            api_key=s.upload_post_api_key,
            user=user,
            target_platform=target,
            caption=caption,
            extras=_linkedin_extras(cfg_block) if target == "linkedin" else {},
        )
        return f"{_WEBHOOK_PENDING_PREFIX}{request_id}", None

    # ── Image post ───────────────────────────────────────────────────────────
    if content_type == "image":
        image_path = cfg_block.get("image_path") or file_path
        if not image_path:
            raise ValueError(
                f"upload_post.publish: content_type=image on {platform!r} "
                "requires platforms.<key>.image_path in brand config or a file_path"
            )
        request_id = await asyncio.to_thread(
            _submit_image,
            api_key=s.upload_post_api_key,
            user=user,
            target_platform=target,
            image_path=image_path,
            caption=caption,
            extras=_linkedin_extras(cfg_block) if target == "linkedin" else {},
        )
        return f"{_WEBHOOK_PENDING_PREFIX}{request_id}", None

    # ── Document / PDF carousel ──────────────────────────────────────────────
    if content_type == "document":
        document_path = cfg_block.get("document_path")
        if not document_path:
            raise ValueError(
                f"upload_post.publish: content_type=document on {platform!r} "
                "requires platforms.<key>.document_path in brand config"
            )
        li_extras: dict = {}
        if cfg_block.get("target_linkedin_page_id"):
            li_extras["target_linkedin_page_id"] = cfg_block["target_linkedin_page_id"]
        if cfg_block.get("visibility"):
            li_extras["visibility"] = cfg_block["visibility"]
        request_id = await asyncio.to_thread(
            _submit_document,
            api_key=s.upload_post_api_key,
            user=user,
            document_path=document_path,
            title=title,
            caption=caption,
            extras=li_extras,
        )
        return f"{_WEBHOOK_PENDING_PREFIX}{request_id}", None

    # ── Video (default) ──────────────────────────────────────────────────────
    if not file_path:
        raise ValueError(f"upload_post.publish: file_path is required for content_type=video on {platform!r}")

    path = pathlib.Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"upload_post.publish: file missing: {file_path}")

    # Retry-path idempotency: if this is the 2nd+ attempt, check whether a
    # prior attempt already published successfully on Upload-Post's side.
    # We match recent history entries on (user, target_platform, caption).
    if attempts > 1:
        recovered = await asyncio.to_thread(
            _lookup_recent_by_caption,
            api_key=s.upload_post_api_key,
            user=user,
            target_platform=target,
            caption=caption,
        )
        if recovered:
            ppid, url = recovered
            log.info(
                "upload_post.publish.recovered_from_history",
                brand_id=brand_id,
                target=target,
                user=user,
                attempts=attempts,
                platform_post_id=ppid,
                share_url=url,
            )
            return ppid, url

    video_url = _build_signed_media_url(path)

    log.info(
        "upload_post.publish.media_url_issued",
        brand_id=brand_id,
        file_path=str(path),
        media_url_host=video_url.split("/")[2] if "://" in video_url else video_url,
        target=target,
        user=user,
    )

    # Upload-Post SDK is blocking requests-based → run in a thread.
    # We DO NOT poll get_status here. upload_video() returns a request_id
    # as soon as UP has accepted the job; the real per-platform post_id and
    # URL arrive later via the upload_completed webhook, handled in
    # server.py::upload_post_webhook. This keeps the scheduler event loop
    # responsive even when UP's transcoding takes minutes.
    request_id = await asyncio.to_thread(
        _submit_upload,
        api_key=s.upload_post_api_key,
        user=user,
        target_platform=target,
        video_url=video_url,
        caption=caption,
        title=title,
        extras=_platform_extras(target, cfg_block),
    )
    return f"{_WEBHOOK_PENDING_PREFIX}{request_id}", None


# ---------------------------------------------------------------------------
# History-based retry short-circuit
# ---------------------------------------------------------------------------

def _lookup_recent_by_caption(
    *,
    api_key: str,
    user: str,
    target_platform: str,
    caption: str,
) -> tuple[str, str | None] | None:
    """Return (platform_post_id, url) of a recent matching upload, if any.

    Upload-Post's get_history() returns posts across all users on the API
    key, so we filter by `user` (profile name). We match by caption equality
    against the `description`/`text` field that the SDK echoes back.
    """
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    try:
        history = client.get_history(page=1, limit=20)
    except Exception as exc:
        log.warning(
            "upload_post.history.lookup_failed",
            user=user,
            target=target_platform,
            error=str(exc)[:200],
        )
        return None

    entries = []
    if isinstance(history, dict):
        entries = history.get("history") or history.get("results") or history.get("posts") or []
    elif isinstance(history, list):
        entries = history

    target_caption = (caption or "").strip()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_user = entry.get("user") or entry.get("username") or entry.get("profile")
        if entry_user and entry_user != user:
            continue
        desc = (
            entry.get("description")
            or entry.get("text")
            or entry.get("caption")
            or ""
        ).strip()
        if target_caption and desc != target_caption:
            continue
        # Extract per-platform result for our target.
        results = entry.get("results") or entry.get("platforms") or []
        if isinstance(results, dict):
            results = [{**(v or {}), "platform": k} for k, v in results.items()]
        for r in results:
            if not isinstance(r, dict):
                continue
            if r.get("platform") != target_platform:
                continue
            ppid = r.get("platform_post_id") or r.get("platformPostId")
            url = r.get("post_url") or r.get("url") or r.get("share_url")
            if ppid or url:
                return ppid or entry.get("request_id") or "", url
    return None


# ---------------------------------------------------------------------------
# Blocking worker
# ---------------------------------------------------------------------------

def _submit_upload(
    *,
    api_key: str,
    user: str,
    target_platform: str,
    video_url: str,
    caption: str,
    title: str,
    extras: dict,
) -> str:
    """Hand the video to Upload-Post. Return the vendor's request_id.

    Does NOT wait for the per-platform publish to complete — that happens
    asynchronously inside Upload-Post. The `upload_completed` webhook (or
    fallback `poll_status_for_request`) will finalize the ScheduledPost
    when the publish finishes.
    """
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)

    # Upload-Post accepts either a local path OR a URL for video_path.
    # We pass our signed URL so the 80+ MB file isn't re-streamed from our
    # server through Python → their API. Upload-Post fetches directly.
    kwargs: dict = dict(
        video_path=video_url,
        user=user,
        platforms=[target_platform],
    )
    # Field mapping for the Upload-Post SDK is platform-dependent:
    #
    # - YouTube / Pinterest / LinkedIn / Reddit:
    #     title       → real title / post-title field
    #     description → body / description field
    #
    # - TikTok / Instagram / X / Threads / Bluesky:
    #     title       → THE CAPTION BODY (no separate description concept)
    #     description → sent, but silently ignored on the TikTok side
    #
    # The earlier code sent the caption as `description` for TikTok,
    # which landed the caption in an Upload-Post internal field but
    # never reached TikTok's post — the description column on the post
    # UI stayed empty. Fix: for title-less platforms, the caption goes
    # into `title`. Upload-Post doesn't have a "title is a title"
    # concept on these platforms — their `title` IS the caption field.
    if target_platform in _TITLE_IS_CAPTION:
        if caption:
            kwargs["title"] = caption
    else:
        if title:
            kwargs["title"] = title
        if caption:
            kwargs["description"] = caption
    kwargs.update(extras)

    resp = client.upload_video(**kwargs)
    if not resp.get("success", True):
        raise RuntimeError(f"Upload-Post upload_video failed: {resp}")

    request_id = (
        resp.get("request_id")
        or (resp.get("results", {}) or {}).get("request_id")
        or str(uuid.uuid4())
    )
    log.info(
        "upload_post.publish.submitted",
        target=target_platform,
        user=user,
        request_id=request_id,
    )
    return request_id


def _submit_text(
    *,
    api_key: str,
    user: str,
    target_platform: str,
    caption: str,
    extras: dict,
) -> str:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    resp = client.upload_text(title=caption, user=user, platforms=[target_platform], **extras)
    if not resp.get("success", True):
        raise RuntimeError(f"Upload-Post upload_text failed: {resp}")
    request_id = (
        resp.get("request_id")
        or (resp.get("results", {}) or {}).get("request_id")
        or str(uuid.uuid4())
    )
    log.info("upload_post.publish.text_submitted", target=target_platform, user=user, request_id=request_id)
    return request_id


def _submit_image(
    *,
    api_key: str,
    user: str,
    target_platform: str,
    image_path: str,
    caption: str,
    extras: dict,
) -> str:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    resp = client.upload_photos(
        photos=[image_path], title=caption, user=user, platforms=[target_platform], **extras
    )
    if not resp.get("success", True):
        raise RuntimeError(f"Upload-Post upload_photos failed: {resp}")
    request_id = (
        resp.get("request_id")
        or (resp.get("results", {}) or {}).get("request_id")
        or str(uuid.uuid4())
    )
    log.info("upload_post.publish.image_submitted", target=target_platform, user=user, request_id=request_id)
    return request_id


def _submit_document(
    *,
    api_key: str,
    user: str,
    document_path: str,
    title: str,
    caption: str,
    extras: dict,
) -> str:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    resp = client.upload_document(
        document_path=document_path, title=title, user=user, description=caption, **extras
    )
    if not resp.get("success", True):
        raise RuntimeError(f"Upload-Post upload_document failed: {resp}")
    request_id = (
        resp.get("request_id")
        or (resp.get("results", {}) or {}).get("request_id")
        or str(uuid.uuid4())
    )
    log.info("upload_post.publish.document_submitted", user=user, request_id=request_id)
    return request_id


def _linkedin_extras(cfg_block: dict) -> dict:
    extras: dict = {}
    if cfg_block.get("target_linkedin_page_id"):
        extras["target_linkedin_page_id"] = cfg_block["target_linkedin_page_id"]
    if cfg_block.get("linkedin_link_url"):
        extras["linkedin_link_url"] = cfg_block["linkedin_link_url"]
    return extras


async def poll_status_for_request(
    request_id: str, target_platform: str
) -> tuple[str | None, str | None]:
    """Ask Upload-Post for the current status of an already-submitted request.

    Used as a fallback by the reconciliation sweep when a webhook doesn't
    arrive within UPLOAD_POST_WEBHOOK_RECONCILE_AFTER_S. Returns
    (platform_post_id, share_url), either of which may be None if the
    publish is still in flight or failed without a post_id.

    Upload-Post status shape:
      {
        "status": "completed",
        "completed": 1, "total": 1,
        "results": [
          {
            "platform": "tiktok",
            "success": true,
            "platform_post_id": "<platform-native post id>",
            "post_url": "<full share URL>",
            ...
          }
        ]
      }
    """
    s = settings()
    if not s.upload_post_api_key:
        raise RuntimeError("UPLOAD_POST_API_KEY is not set")

    return await asyncio.to_thread(
        _poll_once,
        api_key=s.upload_post_api_key,
        request_id=request_id,
        target_platform=target_platform,
    )


def _poll_once(*, api_key: str, request_id: str, target_platform: str) -> tuple[str | None, str | None]:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    try:
        st = client.get_status(request_id=request_id)
    except Exception as exc:
        log.warning(
            "upload_post.reconcile.status_failed",
            request_id=request_id,
            error=str(exc)[:200],
        )
        return None, None

    results = (st or {}).get("results") or []
    if isinstance(results, dict):
        results = [{**(v or {}), "platform": k} for k, v in results.items()]
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("platform") != target_platform:
            continue
        ppid = r.get("platform_post_id") or r.get("platformPostId")
        url = r.get("post_url") or r.get("url") or r.get("share_url")
        err = r.get("error_message") or r.get("errorMessage")
        if err and not ppid:
            raise RuntimeError(f"Upload-Post publish failed on {target_platform}: {err}")
        if ppid or url:
            return ppid, url
    return None, None


# ---------------------------------------------------------------------------
# Webhook event parsing — shared with server.py handler
# ---------------------------------------------------------------------------

def extract_post_from_event(event: dict, target_platform: str) -> tuple[str | None, str | None, str | None]:
    """Pull (platform_post_id, share_url, error_message) from an upload_completed event.

    Accepts both top-level `results` list shapes and the `result` singleton
    shape documented for Upload-Post webhooks. Does not raise — returns
    Nones for any field the payload is missing.
    """
    results = event.get("results") or event.get("result") or []
    if isinstance(results, dict):
        # `result: {success, platform_post_id, post_url, ...}` one-up shape.
        if "platform" not in results:
            results = [{**results, "platform": target_platform}]
        else:
            results = [results]
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("platform") and r.get("platform") != target_platform:
            continue
        ppid = r.get("platform_post_id") or r.get("platformPostId")
        url = r.get("post_url") or r.get("url") or r.get("share_url") or r.get("published_url")
        err = r.get("error_message") or r.get("errorMessage") or r.get("error")
        return ppid, url, err
    return None, None, None


# ---------------------------------------------------------------------------
# Platform-specific extras — pull TikTok/IG/YT settings from brand cfg
# ---------------------------------------------------------------------------

def _platform_extras(target: str, cfg_block: dict) -> dict:
    """Map brand config keys to Upload-Post's platform-specific kwargs."""
    if target == "tiktok":
        extras: dict = {
            "privacy_level": cfg_block.get("default_privacy_level", "PUBLIC_TO_EVERYONE"),
            "disable_duet": bool(cfg_block.get("disable_duet", False)),
            "disable_stitch": bool(cfg_block.get("disable_stitch", False)),
            "disable_comment": bool(cfg_block.get("disable_comment", False)),
            "cover_timestamp": int(cfg_block.get("video_cover_timestamp_ms", 1000)),
        }
        if cfg_block.get("post_mode"):
            extras["post_mode"] = cfg_block["post_mode"]
        if cfg_block.get("is_aigc") is not None:
            extras["is_aigc"] = bool(cfg_block["is_aigc"])
        return extras
    if target == "instagram":
        return {
            "media_type": cfg_block.get("media_type", "REELS"),
            **({"share_to_feed": True} if cfg_block.get("share_to_feed", True) else {}),
        }
    if target == "youtube":
        return {
            "privacyStatus": cfg_block.get("privacy_status", "public"),
            "categoryId": cfg_block.get("category_id", "22"),
        }
    return {}


# ---------------------------------------------------------------------------
# Signed URL (same HMAC scheme used by platforms/zernio.py)
# ---------------------------------------------------------------------------

def _build_signed_media_url(local_path: pathlib.Path) -> str:
    """Return an HMAC-signed public URL served by /media/fetch."""
    s = settings()
    token = make_state_token(
        {"p": str(local_path.resolve()), "k": "media"},
        ttl_s=_MEDIA_URL_TTL_S,
    )
    base = s.media_public_base_url.rstrip("/")
    return f"{base}/media/fetch?token={token}"


# ---------------------------------------------------------------------------
# Caption + title extraction from ContentScript
# ---------------------------------------------------------------------------

async def _read_caption(
    script_id: str, brand_id: str, cfg_block: dict
) -> tuple[str, str, list[str]]:
    """Pull caption + title + hashtags out of the ContentScript row."""
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
