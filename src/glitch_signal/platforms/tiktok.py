"""TikTok Content Posting API publisher (direct post mode).

Flow:
  1. POST /v2/post/publish/video/init/   — reserve publish_id + upload_url
  2. PUT  upload_url                     — chunked binary upload
  3. POLL /v2/post/publish/status/fetch/ — wait for PUBLISH_COMPLETE

Gated behind DISPATCH_MODE=dry_run — in dry-run mode the publisher logs the
intended call and returns a synthetic publish_id.

Requires the `video.publish` scope. `video.upload` alone only puts the video
in the creator's inbox (they still have to tap Post) — for inbox-only mode
swap the init endpoint to /v2/post/publish/inbox/video/init/.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid

import httpx
import structlog

from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import ContentScript
from glitch_signal.db.session import _session_factory
from glitch_signal.oauth.tiktok import get_fresh_access_token

log = structlog.get_logger(__name__)

_INIT_PATH = "/v2/post/publish/video/init/"
_STATUS_PATH = "/v2/post/publish/status/fetch/"

_CHUNK_SIZE = 10 * 1024 * 1024        # 10 MB — target chunk size
_MIN_CHUNK_SIZE = 5 * 1024 * 1024     # 5 MB  — TikTok per-chunk floor
_MAX_CHUNK_SIZE = 64 * 1024 * 1024    # 64 MB — TikTok per-chunk ceiling
# If the whole file is ≤ this, TikTok wants a single-chunk upload.
_SINGLE_CHUNK_MAX = 64 * 1024 * 1024


def _plan_chunks(file_size: int) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) matching TikTok's rules.

    TikTok Content Posting API constraints (2026-04):
      - Each chunk except the LAST must equal chunk_size exactly.
      - Every chunk (including the last) must be in [5 MB, 64 MB] unless
        total_chunk_count == 1 (single-shot upload).
      - For files ≤ 64 MB use a single chunk of file_size bytes.

    For multi-chunk files we floor-divide to leave the remainder rolled
    into the final chunk, guaranteeing chunk_last ≥ chunk_size ≥ 5 MB.
    """
    if file_size <= _SINGLE_CHUNK_MAX:
        return file_size, 1

    chunk_size = _CHUNK_SIZE
    # Floor-divide so the remainder accumulates onto the final chunk
    # instead of becoming a too-small standalone chunk.
    total_chunk_count = max(1, file_size // chunk_size)

    # Pathological case: if the final chunk would exceed the 64 MB ceiling,
    # grow chunk_size until it fits. Only matters for files above ~640 MB
    # with a pathological modulo, and TikTok caps video uploads well below
    # the level where we'd need to worry — but belt-and-braces.
    while (file_size - chunk_size * (total_chunk_count - 1)) > _MAX_CHUNK_SIZE:
        chunk_size += _CHUNK_SIZE
        total_chunk_count = max(1, file_size // chunk_size)

    return chunk_size, total_chunk_count


async def publish(
    file_path: str,
    script_id: str,
    brand_id: str | None = None,
) -> tuple[str, str | None]:
    """Publish a video to TikTok. Returns (publish_id, share_url|None)."""
    s = settings()

    if s.is_dry_run:
        fake_id = f"tiktok-dry-{uuid.uuid4().hex[:10]}"
        log.info(
            "tiktok.publish.dry_run",
            publish_id=fake_id,
            file_path=file_path,
            brand_id=brand_id,
        )
        return fake_id, None

    if not brand_id:
        raise ValueError("tiktok.publish: brand_id is required for live publish")

    path = pathlib.Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"tiktok.publish: file missing: {file_path}")

    file_size = path.stat().st_size
    access_token = await get_fresh_access_token(brand_id)
    cfg = brand_config(brand_id).get("platforms", {}).get("tiktok", {}) or {}

    # Build post metadata from the script + brand config.
    title, caption, tags = await _build_post_metadata(script_id, brand_id, cfg)

    # 1. Init upload.
    init_resp = await _init_upload(
        access_token=access_token,
        file_size=file_size,
        title=title,
        caption=caption,
        tags=tags,
        cfg=cfg,
    )
    publish_id = init_resp["publish_id"]
    upload_url = init_resp["upload_url"]

    # 2. Upload file (single PUT for ≤10MB; chunked otherwise).
    await _upload_file(upload_url, path, file_size)

    # 3. Poll status until PUBLISH_COMPLETE or timeout.
    share_url = await _poll_until_published(access_token, publish_id)

    log.info(
        "tiktok.publish.done",
        publish_id=publish_id,
        share_url=share_url,
        brand_id=brand_id,
    )
    return publish_id, share_url


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _init_upload(
    *,
    access_token: str,
    file_size: int,
    title: str,
    caption: str,
    tags: list[str],
    cfg: dict,
) -> dict:
    s = settings()
    chunk_size, total_chunk_count = _plan_chunks(file_size)

    body = {
        "post_info": {
            "title": (caption or title)[:2200],   # TikTok caption limit
            "privacy_level": cfg.get("default_privacy_level", "PUBLIC_TO_EVERYONE"),
            "disable_duet": bool(cfg.get("disable_duet", False)),
            "disable_stitch": bool(cfg.get("disable_stitch", False)),
            "disable_comment": bool(cfg.get("disable_comment", False)),
            "video_cover_timestamp_ms": int(cfg.get("video_cover_timestamp_ms", 1000)),
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{s.tiktok_api_base}{_INIT_PATH}",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
    payload = _safe_json(resp)
    if resp.status_code >= 400 or payload.get("error", {}).get("code") not in (None, "ok"):
        log.error("tiktok.init.failed", status=resp.status_code, body=payload)
        raise RuntimeError(f"TikTok init failed: {payload}")
    data = payload.get("data") or {}
    if not data.get("publish_id") or not data.get("upload_url"):
        raise RuntimeError(f"TikTok init response missing fields: {payload}")
    return data


async def _upload_file(upload_url: str, path: pathlib.Path, file_size: int) -> None:
    chunk_size, total_chunk_count = _plan_chunks(file_size)

    async with httpx.AsyncClient(timeout=600) as client:
        if total_chunk_count == 1:
            # Single-shot upload.
            with path.open("rb") as f:
                data = f.read()
            resp = await client.put(
                upload_url,
                content=data,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
                    "Content-Length": str(file_size),
                },
            )
            if resp.status_code >= 300:
                log.error("tiktok.upload.failed", status=resp.status_code, body=resp.text[:500])
                raise RuntimeError(f"TikTok upload failed: HTTP {resp.status_code}")
            return

        # Chunked upload — send exactly total_chunk_count chunks. Every
        # chunk except the last is exactly chunk_size bytes. The final
        # chunk absorbs any remainder, so it's ≥ chunk_size ≥ 5 MB.
        with path.open("rb") as f:
            for i in range(total_chunk_count):
                if i == total_chunk_count - 1:
                    # Final chunk — take whatever is left.
                    chunk = f.read()
                else:
                    chunk = f.read(chunk_size)
                if not chunk:
                    break
                offset = i * chunk_size
                end = offset + len(chunk) - 1
                resp = await client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end}/{file_size}",
                        "Content-Length": str(len(chunk)),
                    },
                )
                if resp.status_code >= 300 and resp.status_code != 206:
                    log.error(
                        "tiktok.upload.chunk_failed",
                        offset=offset,
                        status=resp.status_code,
                        body=resp.text[:500],
                    )
                    raise RuntimeError(
                        f"TikTok chunk upload failed at offset={offset}: HTTP {resp.status_code}"
                    )


async def _poll_until_published(access_token: str, publish_id: str) -> str | None:
    s = settings()
    deadline_s = s.tiktok_post_status_timeout_s
    elapsed = 0
    interval = 5

    while elapsed < deadline_s:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{s.tiktok_api_base}{_STATUS_PATH}",
                json={"publish_id": publish_id},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )
        payload = _safe_json(resp)
        data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
        status = str(data.get("status", ""))

        if status == "PUBLISH_COMPLETE":
            publicaly_available_post_id = data.get("publicaly_available_post_id") or []
            share_url = data.get("share_url")
            if share_url:
                return share_url
            if publicaly_available_post_id:
                # Build a best-effort URL; openness of this identifier varies by account.
                return f"https://www.tiktok.com/video/{publicaly_available_post_id[0]}"
            return None

        if status.startswith("FAIL") or status == "PROCESSING_DOWNLOAD" and data.get("fail_reason"):
            raise RuntimeError(f"TikTok publish failed: {data}")

        await asyncio.sleep(interval)
        elapsed += interval

    raise RuntimeError(
        f"TikTok publish status timed out after {deadline_s}s "
        f"for publish_id={publish_id} — video may still complete; check TikTok Studio"
    )


async def _build_post_metadata(
    script_id: str,
    brand_id: str,
    cfg: dict,
) -> tuple[str, str, list[str]]:
    factory = _session_factory()
    async with factory() as session:
        cs = await session.get(ContentScript, script_id) if script_id else None

    default_tags: list[str] = (
        cfg.get("default_tags")
        or brand_config(brand_id).get("default_hashtags", [])
        or []
    )
    # Normalise "#tag" → "tag" (TikTok expects plain tokens in post_info).
    clean_tags = [t.lstrip("#") for t in default_tags if t]

    if cs and cs.script_body:
        # First sentence as title; full body (trimmed) as caption.
        first_sentence = cs.script_body.split(".")[0][:100].strip()
        title = first_sentence or cs.script_body[:100].strip()
        hashtag_block = " ".join(f"#{t}" for t in clean_tags)
        caption = (cs.script_body[:2100].strip() + ("\n\n" + hashtag_block if hashtag_block else "")).strip()
    else:
        title = brand_config(brand_id).get("display_name", brand_id)
        caption = " ".join(f"#{t}" for t in clean_tags)

    return title, caption, clean_tags


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"code": "non_json", "message": resp.text[:500]}}
