"""Pull per-post analytics from Upload-Post into MetricsSnapshot.

Upload-Post exposes per-post metrics via:

  GET /api/uploadposts/post-analytics/{request_id}            # by our upload id
  GET /api/uploadposts/post-analytics                         # by (platform_post_id, platform, user)

We use the second form because:
  - It works for BOTH posts we made (we know the UP request_id) AND posts
    that show up in history without one.
  - The native platform_post_id is already on PublishedPost — no extra
    bookkeeping needed.

Metric normalization: each platform uses different field names for the
same concept (views / view_count / play_count / impressions / …). We
coalesce defensively so the agent's learning loop gets a consistent
schema regardless of which platform the post went to.

Trigger: the scheduler's `_pull_post_analytics` tick calls
`sweep_due_posts()` every tick. It picks eligible PublishedPost rows
and writes a MetricsSnapshot per post per pull.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import MetricsSnapshot, PublishedPost
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Platform key resolution — tiny duplicate of media/ffmpeg.canonical_platform.
# Kept local so analytics doesn't depend on the ffmpeg module landing first.
# ---------------------------------------------------------------------------

def _canonical_platform(platform_key: str) -> str | None:
    """Return Upload-Post's canonical platform name, or None if not UP-backed."""
    if platform_key.startswith("upload_post_"):
        return platform_key[len("upload_post_"):]
    return None


# ---------------------------------------------------------------------------
# Metric field coalescing — Upload-Post's shape varies per platform, so we
# probe a prioritised list of keys for each concept. Unknown platforms
# still get best-effort numbers instead of zeros.
# ---------------------------------------------------------------------------

_VIEW_KEYS     = ("views", "view_count", "video_views", "play_count", "plays", "impressions", "impression_count")
_LIKE_KEYS     = ("likes", "like_count", "favorites", "favorite_count", "reactions")
_COMMENT_KEYS  = ("comments", "comment_count", "replies")
_SHARE_KEYS    = ("shares", "share_count", "reposts", "retweets", "retweet_count")


def _pick_int(source: dict, keys: Iterable[str]) -> int:
    for k in keys:
        v = source.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return 0


def extract_metrics(payload: dict, platform: str) -> dict:
    """Normalise Upload-Post's per-post analytics dict to {views,likes,comments,shares}.

    Handles both the shallow shape `{views, likes, ...}` and the nested
    `{metrics: {...}}` / `{data: {...}}` / `{result: {...}}` shapes we've
    seen in the wild.
    """
    if not isinstance(payload, dict):
        return {"views": 0, "likes": 0, "comments": 0, "shares": 0}

    # If there's a per-platform block keyed by platform name, prefer that.
    inner = payload.get(platform)
    if not isinstance(inner, dict):
        inner = (
            payload.get("metrics")
            or payload.get("data")
            or payload.get("result")
            or payload
        )
    if not isinstance(inner, dict):
        inner = payload

    return {
        "views":    _pick_int(inner, _VIEW_KEYS),
        "likes":    _pick_int(inner, _LIKE_KEYS),
        "comments": _pick_int(inner, _COMMENT_KEYS),
        "shares":   _pick_int(inner, _SHARE_KEYS),
    }


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_metrics_for_post(
    platform_post_id: str, platform: str, user: str
) -> dict:
    """Call Upload-Post's analytics API. Returns normalised metrics dict."""
    s = settings()
    if not s.upload_post_api_key:
        raise RuntimeError("UPLOAD_POST_API_KEY is not set")

    raw = await asyncio.to_thread(
        _fetch_sync,
        api_key=s.upload_post_api_key,
        platform_post_id=platform_post_id,
        platform=platform,
        user=user,
    )
    return extract_metrics(raw or {}, platform)


def _fetch_sync(
    *, api_key: str, platform_post_id: str, platform: str, user: str
) -> dict:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    return client.get_post_analytics_by_platform_id(
        platform_post_id=platform_post_id,
        platform=platform,
        user=user,
    )


# ---------------------------------------------------------------------------
# Sweep — pick eligible PublishedPost rows and write MetricsSnapshots
# ---------------------------------------------------------------------------

async def sweep_due_posts(limit: int = 10) -> list[str]:
    """Pull analytics for up to `limit` PublishedPost rows that are due.

    Eligibility:
      - published_at >= analytics_first_pull_after_s ago (let metrics settle)
      - no MetricsSnapshot OR latest snapshot is older than analytics_pull_interval_s
      - platform is Upload-Post-backed

    Returns the list of PublishedPost ids that got a fresh snapshot.
    """
    s = settings()
    now = datetime.now(UTC).replace(tzinfo=None)
    min_age = now - timedelta(seconds=s.analytics_first_pull_after_s)
    stale_before = now - timedelta(seconds=s.analytics_pull_interval_s)

    factory = _session_factory()
    async with factory() as session:
        # Candidate PublishedPost rows old enough to be worth polling.
        result = await session.execute(
            select(PublishedPost)
            .where(
                PublishedPost.published_at <= min_age,
                PublishedPost.platform.like("upload_post_%"),
            )
            .order_by(PublishedPost.published_at.desc())
            .limit(limit * 3)   # pull extra candidates; we filter by freshness below
        )
        posts = result.scalars().all()

    updated: list[str] = []
    for pub in posts:
        if len(updated) >= limit:
            break
        if not await _should_pull(pub.id, stale_before):
            continue

        platform_canonical = _canonical_platform(pub.platform)
        if not platform_canonical:
            continue

        user = _resolve_user(pub.brand_id, pub.platform)
        if not user:
            log.warning(
                "analytics.skip_missing_user",
                published_post_id=pub.id,
                brand_id=pub.brand_id,
                platform=pub.platform,
            )
            continue

        try:
            metrics = await fetch_metrics_for_post(
                platform_post_id=pub.platform_post_id,
                platform=platform_canonical,
                user=user,
            )
        except Exception as exc:
            log.warning(
                "analytics.fetch_failed",
                published_post_id=pub.id,
                platform=platform_canonical,
                error=str(exc)[:200],
            )
            continue

        factory = _session_factory()
        async with factory() as session:
            snap = MetricsSnapshot(
                id=str(uuid.uuid4()),
                brand_id=pub.brand_id,
                published_post_id=pub.id,
                captured_at=now,
                **metrics,
            )
            session.add(snap)
            await session.commit()

        log.info(
            "analytics.snapshot_written",
            published_post_id=pub.id,
            platform=platform_canonical,
            **metrics,
        )
        updated.append(pub.id)

    return updated


async def _should_pull(published_post_id: str, stale_before: datetime) -> bool:
    """True if the latest MetricsSnapshot for this post is older than the interval."""
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(MetricsSnapshot)
            .where(MetricsSnapshot.published_post_id == published_post_id)
            .order_by(MetricsSnapshot.captured_at.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()

    if latest is None:
        return True
    return latest.captured_at <= stale_before


def _resolve_user(brand_id: str, platform_key: str) -> str | None:
    """Look up the Upload-Post profile username for this brand + platform."""
    try:
        cfg = brand_config(brand_id)
    except Exception:
        return None
    block = (cfg.get("platforms") or {}).get(platform_key) or {}
    return block.get("user")
