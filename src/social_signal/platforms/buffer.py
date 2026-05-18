"""Buffer publisher — GraphQL-backed TikTok publishing.

Why a third vendor on top of Upload-Post and Zernio:

  Upload-Post's TikTok pipeline silently triggers TikTok's synthetic-media
  audio mute for AI-generated voice content. The same files post cleanly
  on Upload-Post → Instagram and on Buffer → TikTok. Direct-to-origin
  evidence: diagnostic posts on 2026-04-19 where the same byte-identical
  file had muted audio via Upload-Post and full audio via Buffer to the
  same `@glitchexec` TikTok channel.

  Buffer's server forwards our file URL to TikTok's Content Posting API
  *without* server-side re-muxing. The `video_was_transcoded: false`
  on Upload-Post combined with the mute asymmetry points to their
  remux pipeline as the trigger; Buffer avoids this by staying hands-off.

Scope:
  Today this publisher is TikTok-only. Buffer supports Instagram / YouTube
  / LinkedIn / X etc., but their IG path passes files straight through
  (no normalization), which means our 20 Mbps / 100+ MB reels exceed
  Instagram Graph API native limits — Upload-Post's re-encoding is
  actually useful there. Add per-platform coverage later if we hit
  similar issues on non-TikTok targets.

Platform-key convention mirrors zernio_* and upload_post_*:
  buffer_tiktok, buffer_instagram, buffer_youtube, …

Per-brand config lives under platforms.buffer_<target>:
  - enabled: true
  - channel_id: Buffer channel id (get from `channels(input: {organizationId})`)
  - organization_id: Buffer organization id

Webhook-driven finalization:
  createPost returns a Buffer post id immediately with status=sending.
  Buffer uploads to the target platform asynchronously (~30s–3min) and
  status flips to sent/failed. We return a `webhook_pending:<post_id>`
  sentinel so publisher.py flips ScheduledPost to `awaiting_webhook`.
  The reconcile sweep in scheduler/queue.py polls Buffer's post(input)
  query and finalizes when status is sent/failed.

  Buffer has no webhooks on free/basic tiers as of 2026-04. If that
  changes, wire a webhook handler — the plumbing is the same as Upload-
  Post's.

Rate limits:
  Buffer enforces a 24-hour per-client quota on GraphQL requests.
  Observed error shape:
    {"errors":[{"extensions":{"code":"RATE_LIMIT_EXCEEDED","window":"24h"}}]}
  Scale considerations for the reconcile sweep: poll at a slow cadence
  (minutes, not seconds) and batch where possible.

DISPATCH_MODE=dry_run short-circuits without calling the API.
"""
from __future__ import annotations

import pathlib
import uuid

import httpx
import structlog

from social_signal.config import brand_config, settings
from social_signal.crypto import make_state_token
from social_signal.db.models import ContentScript
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

_GRAPHQL_URL = "https://api.buffer.com"

# TTL for the signed URL we hand Buffer. Their ingest worker HEAD-checks
# the URL immediately on createPost, then GETs it during upload to the
# target platform (seconds to a few minutes later). 1 hour is a generous
# ceiling.
_MEDIA_URL_TTL_S = 60 * 60

# Sentinel prefix shared with upload_post.py. publisher.py treats both
# vendors' pending posts uniformly — it stashes the post id in
# scheduled_post.vendor_request_id and flips status to awaiting_webhook.
# The reconcile sweep then routes by sp.platform prefix to the right vendor.
_WEBHOOK_PENDING_PREFIX = "webhook_pending:"

# Short timeout for the submission call. Buffer accepts createPost in
# milliseconds even for large videos — they just register the URL and
# hand off to a worker. A long deadline here means a stuck TCP session
# blocks the scheduler tick.
_SUBMIT_TIMEOUT_S = 30.0
_POLL_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Platform-key mapping
# ---------------------------------------------------------------------------

_PLATFORM_MAP = {
    "buffer_tiktok":    "tiktok",
    "buffer_instagram": "instagram",
    "buffer_youtube":   "youtube",
    "buffer_linkedin":  "linkedin",
    "buffer_facebook":  "facebook",
    "buffer_x":         "x",
    "buffer_threads":   "threads",
    "buffer_pinterest": "pinterest",
    "buffer_bluesky":   "bluesky",
}


def is_webhook_pending(token: str) -> bool:
    """True if publish() returned a pending sentinel rather than a finalized id."""
    return isinstance(token, str) and token.startswith(_WEBHOOK_PENDING_PREFIX)


def extract_post_id(token: str) -> str:
    """Return the Buffer post id from a pending sentinel."""
    if not is_webhook_pending(token):
        raise ValueError(f"Not a webhook-pending sentinel: {token!r}")
    return token[len(_WEBHOOK_PENDING_PREFIX):]


# ---------------------------------------------------------------------------
# Publish entry point
# ---------------------------------------------------------------------------

async def publish(
    platform: str,
    file_path: str,
    script_id: str,
    brand_id: str | None = None,
    attempts: int = 1,
) -> tuple[str, str | None]:
    """Publish a video via Buffer. Returns (sentinel, None).

    The first return is a `webhook_pending:<buffer_post_id>` token; the
    reconcile sweep pulls the real per-platform post URL later via
    `poll_status_for_post`. `attempts` is currently unused — Buffer
    dedupes server-side on its own post ids, so our scheduler's retry
    with the same ScheduledPost never produces a duplicate createPost
    on Buffer's side provided we cache the post id we got back.
    """
    s = settings()

    if s.is_dry_run:
        fake_id = f"buffer-dry-{uuid.uuid4().hex[:10]}"
        log.info(
            "buffer.publish.dry_run",
            publish_id=fake_id,
            file_path=file_path,
            brand_id=brand_id,
            platform=platform,
        )
        return fake_id, None

    if not brand_id:
        raise ValueError("buffer.publish: brand_id is required for live publish")
    if not s.buffer_api_token:
        raise RuntimeError("BUFFER_API_TOKEN is not set")

    target = _PLATFORM_MAP.get(platform)
    if not target:
        raise ValueError(f"buffer.publish: unknown platform key {platform!r}")
    if target != "tiktok":
        # See module docstring: we deliberately only support TikTok for
        # now. Extending to IG/YT requires pre-encoding to fit native
        # API limits that Upload-Post normalises on our behalf today.
        raise NotImplementedError(
            f"buffer.publish: only tiktok is supported today (got target={target!r})"
        )

    cfg_block = (brand_config(brand_id).get("platforms", {}).get(platform, {}) or {})
    channel_id = cfg_block.get("channel_id")
    organization_id = cfg_block.get("organization_id")
    if not channel_id:
        raise RuntimeError(
            f"buffer.publish: brand={brand_id!r} is missing "
            f"platforms.{platform}.channel_id — get it via Buffer's "
            f"channels(input:{{organizationId}}) query and add to the brand config"
        )
    if not organization_id:
        raise RuntimeError(
            f"buffer.publish: brand={brand_id!r} is missing "
            f"platforms.{platform}.organization_id"
        )

    path = pathlib.Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"buffer.publish: file missing: {file_path}")

    caption = await _read_caption(script_id)
    video_url = _build_signed_media_url(path)

    log.info(
        "buffer.publish.media_url_issued",
        brand_id=brand_id,
        file_path=str(path),
        media_url_host=video_url.split("/")[2] if "://" in video_url else video_url,
        target=target,
        channel_id=channel_id,
    )

    variables = {
        "input": {
            "channelId": channel_id,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "text": caption or "",
            "assets": {"videos": [{"url": video_url}]},
            "source": "ai-social-media-agent",
        }
    }
    query = (
        "mutation($input: CreatePostInput!) {"
        "  createPost(input: $input) {"
        "    __typename"
        "    ... on PostActionSuccess { post { id status } }"
        "    ... on InvalidInputError { message }"
        "    ... on UnauthorizedError { message }"
        "    ... on LimitReachedError { message }"
        "    ... on NotFoundError { message }"
        "    ... on UnexpectedError { message }"
        "    ... on RestProxyError { message }"
        "  }"
        "}"
    )

    async with httpx.AsyncClient(timeout=_SUBMIT_TIMEOUT_S) as client:
        resp = await client.post(
            _GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {s.buffer_api_token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
        )
    resp.raise_for_status()
    body = resp.json()

    if body.get("errors"):
        raise RuntimeError(f"Buffer createPost failed: {body['errors']}")

    payload = (body.get("data") or {}).get("createPost") or {}
    typename = payload.get("__typename")
    if typename != "PostActionSuccess":
        msg = payload.get("message") or "no detail"
        raise RuntimeError(f"Buffer createPost returned {typename}: {msg}")

    post = payload.get("post") or {}
    post_id = post.get("id")
    status = post.get("status")
    if not post_id:
        raise RuntimeError(f"Buffer createPost succeeded but no post.id in response: {payload}")

    log.info(
        "buffer.publish.submitted",
        brand_id=brand_id,
        target=target,
        channel_id=channel_id,
        buffer_post_id=post_id,
        buffer_status=status,
    )
    return f"{_WEBHOOK_PENDING_PREFIX}{post_id}", None


# ---------------------------------------------------------------------------
# Reconciliation — polled by scheduler/queue.py for awaiting_webhook rows
# ---------------------------------------------------------------------------

async def poll_status_for_post(
    buffer_post_id: str, organization_id: str | None = None
) -> tuple[str | None, str | None]:
    """Return (platform_post_id, share_url) for a Buffer post, or (None, None).

    Called by the reconcile sweep. A None return means "still in flight,
    try again next tick". A RuntimeError means "Buffer rejected the post"
    and caller should mark ScheduledPost as failed.

    Buffer's Post type exposes `status` (sending/sent/failed/…) and
    `externalLink` (the native TikTok URL once published). We don't get
    the per-platform post id separately — the externalLink carries it
    in the path, which is enough for sheet tracking and observability.

    `organization_id` is accepted for API symmetry with the brand-config
    caller but is NOT part of Buffer's PostInput schema — the post id
    alone is the key. The param stays to keep the scheduler callsite
    uniform; remove once we're confident no branch relies on it.
    """
    del organization_id  # retained for caller symmetry, unused in query

    s = settings()
    if not s.buffer_api_token:
        raise RuntimeError("BUFFER_API_TOKEN is not set")

    query = (
        "query($input: PostInput!) {"
        "  post(input: $input) {"
        "    id status externalLink channelService"
        "  }"
        "}"
    )
    # Buffer's PostInput is {id} only — organizationId is not in its schema.
    variables = {"input": {"id": buffer_post_id}}

    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_S) as client:
        resp = await client.post(
            _GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {s.buffer_api_token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
        )
    resp.raise_for_status()
    body = resp.json()

    if body.get("errors"):
        # Rate limit errors bubble up here. The reconcile caller treats
        # RuntimeError as "don't mark failed yet, retry next tick" — let
        # the exception propagate with enough detail for the log line.
        raise RuntimeError(f"Buffer post() query failed: {body['errors']}")

    post = (body.get("data") or {}).get("post") or {}
    status = post.get("status")
    external = post.get("externalLink")

    if status == "sent":
        return buffer_post_id, external
    if status in ("failed", "error"):
        raise RuntimeError(
            f"Buffer post {buffer_post_id!r} reported status={status!r}"
        )
    # sending / processing / unknown → still in flight
    return None, None


# ---------------------------------------------------------------------------
# Caption extraction — reads ContentScript.script_body by script_id
# ---------------------------------------------------------------------------

async def _read_caption(script_id: str | None) -> str:
    """Return the caption body for the post, or empty string if not found.

    Mirrors upload_post._read_caption but returns just the caption — Buffer
    only has one text field on createPost (no title/description split),
    so hashtag extraction + title derivation aren't needed here.
    """
    if not script_id:
        return ""
    factory = _session_factory()
    async with factory() as session:
        cs = await session.get(ContentScript, script_id)
    return (cs.script_body if cs else "").strip()


# ---------------------------------------------------------------------------
# Signed media URL (shares scheme with upload_post.py / zernio.py)
# ---------------------------------------------------------------------------

def _build_signed_media_url(local_path: pathlib.Path) -> str:
    """Return an HMAC-signed public URL served by /media/fetch.

    Buffer validates the URL with a HEAD request before accepting the
    post (see server.py::media_fetch_head). The GET happens later when
    Buffer's worker uploads to TikTok.
    """
    s = settings()
    token = make_state_token(
        {"p": str(local_path.resolve()), "k": "media"},
        ttl_s=_MEDIA_URL_TTL_S,
    )
    base = s.media_public_base_url.rstrip("/")
    return f"{base}/media/fetch?token={token}"
