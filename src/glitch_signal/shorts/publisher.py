"""Upload a rendered Short to YouTube via Upload-Post.

Uses the same Upload-Post account + monthly subscription that already
publishes our LinkedIn carousels and X tweets — no new vendor, no extra
cost beyond what we're already paying.

Flow:
  1. Resolve the brand's YouTube user from brand_config["platforms"]
     ["upload_post_youtube"]["user"]   (e.g. "Glitch")
  2. Call client.upload_video(video_path, title, user, platforms=["youtube"])
  3. Block on the async upload via get_status until YouTube returns a
     real video id + url, OR timeout (~3 min) and let the reconciler
     finish the row.

Returns (ok, summary_dict). The summary includes the YouTube video id
+ URL when sync, or a `request:<id>` placeholder when the upload was
accepted async by the worker.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import structlog

from glitch_signal.config import brand_config, settings

log = structlog.get_logger(__name__)


async def upload_short(
    *,
    brand_id: str,
    mp4_path: pathlib.Path,
    title: str,
    description: str = "",
    poll_timeout_s: int = 180,
) -> tuple[bool, dict]:
    """Upload a rendered Short via Upload-Post → YouTube. Returns (ok, info)."""
    if not mp4_path.exists():
        return False, {"error": f"mp4 not found: {mp4_path}"}

    cfg = brand_config(brand_id)
    block = (cfg.get("platforms", {}) or {}).get("upload_post_youtube") or {}
    user = block.get("user")
    if not block.get("enabled") or not user:
        return False, {
            "error": (
                f"{brand_id}.upload_post_youtube not enabled / missing user. "
                "Add the upload_post_youtube block to brand/configs/"
                f"{brand_id}.json with a `user` matching your Upload-Post profile."
            ),
        }

    api_key = settings().upload_post_api_key
    if not api_key:
        return False, {"error": "UPLOAD_POST_API_KEY unset"}

    if settings().is_dry_run:
        log.info(
            "shorts.upload.dry_run",
            brand_id=brand_id, mp4=str(mp4_path), title=title[:80],
        )
        return True, {"dry_run": True, "fake_id": f"dry-{mp4_path.stem[:8]}"}

    log.info(
        "shorts.upload.starting",
        brand_id=brand_id, user=user, title=title[:80],
        size_mb=round(mp4_path.stat().st_size / 1024 / 1024, 1),
    )

    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)

    def _upload() -> dict:
        kwargs: dict = {
            "video_path": str(mp4_path),
            "title": title,
            "user": user,
            "platforms": ["youtube"],
            "description": description,
        }
        # Pull through any optional YouTube-specific tunables the brand set
        for key in (
            "privacy_status",       # public | private | unlisted
            "category_id",          # numeric (default 22 = People & Blogs)
            "tags",                 # list[str]
            "made_for_kids",        # bool
            "notify_subscribers",   # bool
        ):
            if key in block:
                kwargs[key] = block[key]
        return client.upload_video(**kwargs)

    try:
        resp = await asyncio.to_thread(_upload)
    except Exception as exc:
        log.warning("shorts.upload.failed", error=str(exc)[:300])
        return False, {"error": str(exc)[:300]}

    info = _extract(resp)
    request_id = resp.get("request_id") if isinstance(resp, dict) else None

    # If the vendor returned a sync platform_post_id immediately, we're done.
    if info.get("platform_post_id") or info.get("post_url"):
        log.info(
            "shorts.upload.posted_sync",
            brand_id=brand_id, **info,
        )
        return True, info

    # Otherwise poll get_status. Most YouTube uploads finalize in 30-90s.
    if not request_id:
        return False, {"error": "no platform_post_id and no request_id in response", "raw": resp}

    poll_started = time.time()
    while time.time() - poll_started < poll_timeout_s:
        await asyncio.sleep(5)
        try:
            st = await asyncio.to_thread(client.get_status, request_id=request_id)
        except Exception as exc:
            log.warning("shorts.upload.status_poll_failed", error=str(exc)[:200])
            continue
        info = _extract(st or {})
        if info.get("platform_post_id") or info.get("post_url"):
            log.info(
                "shorts.upload.posted_async",
                brand_id=brand_id, request_id=request_id, **info,
                wait_s=round(time.time() - poll_started, 1),
            )
            return True, {"request_id": request_id, **info}

    # Timed out — return the request_id so the reconciler can finish it.
    log.info(
        "shorts.upload.background",
        brand_id=brand_id, request_id=request_id,
        wait_s=poll_timeout_s,
    )
    return True, {
        "request_id": request_id,
        "platform_post_id": f"request:{request_id}",
        "note": "background upload — status confirmed via reconciler",
    }


def _extract(resp: dict) -> dict:
    """Pull (platform_post_id, post_url) from Upload-Post's two response shapes."""
    if not isinstance(resp, dict):
        return {}
    results = resp.get("results") or {}
    block: dict = {}
    if isinstance(results, dict):
        block = results.get("youtube") or {}
    elif isinstance(results, list) and results:
        block = results[0] if isinstance(results[0], dict) else {}

    pid = (
        block.get("platform_post_id")
        or block.get("post_id")
        or block.get("video_id")
    )
    url = block.get("post_url") or block.get("url")
    return {k: v for k, v in {"platform_post_id": pid, "post_url": url}.items() if v}
