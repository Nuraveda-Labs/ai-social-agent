"""Publisher node — routes ScheduledPost to the correct platform client.

Invoked by scheduler/queue.py, not the LangGraph graph directly.
On success: writes PublishedPost, marks ScheduledPost done.
On failure: applies retry backoff (same pattern as cod-confirm handleFailure).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from glitch_signal.config import settings
from glitch_signal.db.models import PublishedPost, ScheduledPost, VideoAsset
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)


async def publish(scheduled_post_id: str) -> None:
    """Entry point called by the scheduler tick."""
    factory = _session_factory()
    async with factory() as session:
        sp = await session.get(ScheduledPost, scheduled_post_id)
        if not sp:
            log.error("publisher.not_found", scheduled_post_id=scheduled_post_id)
            return

        # Idempotency guard — a prior attempt may have written PublishedPost
        # but crashed before flipping scheduled_post.status to "done" (process
        # kill, DB commit blip, etc.). If we see a PublishedPost row, the
        # vendor already posted; do not ask it to post again.
        result = await session.execute(
            select(PublishedPost).where(
                PublishedPost.scheduled_post_id == scheduled_post_id
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            log.info(
                "publisher.already_published",
                scheduled_post_id=scheduled_post_id,
                platform_post_id=existing.platform_post_id,
            )
            sp.status = "done"
            session.add(sp)
            await session.commit()
            return

        # Text-post path: no VideoAsset, publish body comes from ContentScript.
        is_text_post = sp.asset_id is None and sp.script_id is not None
        asset = None
        if not is_text_post:
            asset = await session.get(VideoAsset, sp.asset_id)
            if not asset:
                await _mark_failed(session, sp, "VideoAsset not found")
                return

        sp.status = "dispatching"
        sp.attempts += 1
        sp.last_attempt_at = datetime.now(UTC).replace(tzinfo=None)
        session.add(sp)
        await session.commit()
        attempts_before_call = sp.attempts

    try:
        brand_id = getattr(sp, "brand_id", None) or (
            getattr(asset, "brand_id", None) if asset else None
        )

        if is_text_post:
            # No file transforms, no video asset. upload_post.publish() reads
            # the post body from the ContentScript via script_id when
            # content_type="text" on the platform config.
            platform_post_id, platform_url = await _publish_to_platform(
                sp.platform,
                None,                 # no file_path
                sp.script_id,
                brand_id=brand_id,
                attempts=attempts_before_call,
            )
        else:
            # JIT download — drive_scout no longer pre-downloads; it just
            # records the expected local path. If the file isn't on disk,
            # fetch it from Drive now. Re-runs are a no-op once downloaded.
            await _ensure_local_file(asset)
            # Pre-publish ffmpeg transforms (brand-config driven). Returns the
            # original path unchanged for brands with no `media_pipeline`
            # entry for this platform — zero cost on the common path.
            from glitch_signal.media.ffmpeg import apply_transforms
            publish_file_path = await apply_transforms(
                asset.file_path, brand_id or "", sp.platform
            )
            platform_post_id, platform_url = await _publish_to_platform(
                sp.platform,
                publish_file_path,
                asset.script_id,
                brand_id=brand_id,
                attempts=attempts_before_call,
            )
    except Exception as exc:
        log.error("publisher.failed", scheduled_post_id=scheduled_post_id, error=str(exc))
        await _handle_failure(scheduled_post_id, str(exc))
        return

    # Vendor-async handoff: publishers that finish over webhooks return a
    # `webhook_pending:<request_id>` sentinel instead of a real post_id.
    # We persist the request_id so the webhook handler / reconciliation
    # sweep can correlate the callback back to this ScheduledPost, and
    # flip status to `awaiting_webhook` so the scheduler stops trying to
    # republish it.
    from glitch_signal.platforms.upload_post import (
        extract_request_id,
        is_webhook_pending,
    )
    if is_webhook_pending(platform_post_id):
        request_id = extract_request_id(platform_post_id)
        factory = _session_factory()
        async with factory() as session:
            sp = await session.get(ScheduledPost, scheduled_post_id)
            if sp:
                sp.status = "awaiting_webhook"
                sp.vendor_request_id = request_id
                session.add(sp)
                await session.commit()
        log.info(
            "publisher.awaiting_webhook",
            scheduled_post_id=scheduled_post_id,
            vendor_request_id=request_id,
        )
        return

    factory = _session_factory()
    async with factory() as session:
        sp = await session.get(ScheduledPost, scheduled_post_id)
        if sp:
            sp.status = "done"
            session.add(sp)

        pub = PublishedPost(
            id=str(uuid.uuid4()),
            brand_id=getattr(sp, "brand_id", "glitch_executor") if sp else "glitch_executor",
            scheduled_post_id=scheduled_post_id,
            platform=sp.platform if sp else "unknown",
            platform_post_id=platform_post_id,
            platform_url=platform_url,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(pub)
        await session.commit()

        # Resolve the Drive filename via signal to update the sheet by key.
        video_name = await _resolve_drive_filename_for_asset(session, sp.asset_id if sp else None)

    # Sheet tracker update — posted status + TikTok URL. Done after the
    # DB commit so the DB truth is authoritative even if Sheets is down.
    if video_name and sp:
        from glitch_signal.integrations import sheet_tracker
        await sheet_tracker.update_by_video_name(
            sp.brand_id,
            video_name,
            {
                "status": "posted",
                "posted_at": pub.published_at.isoformat(timespec="seconds"),
                "tiktok_url": platform_url or "",
            },
        )

    log.info(
        "publisher.done",
        scheduled_post_id=scheduled_post_id,
        platform_post_id=platform_post_id,
        url=platform_url,
    )


async def _ensure_local_file(asset: VideoAsset) -> None:
    """Download the asset's Drive file to its expected local path, if absent.

    drive_scout writes the expected local path to VideoAsset.file_path but
    (as of the JIT-download refactor) no longer downloads the file eagerly.
    Publisher calls this right before apply_transforms / upload so the file
    is on disk exactly when needed and nowhere else.

    For non-Drive-sourced assets (ai_generated pipeline, already-rendered
    files), the file_path should already exist — this is a no-op.
    """
    import pathlib

    from glitch_signal.db.models import ContentScript, Signal
    from glitch_signal.integrations import google_drive

    if not asset.file_path:
        return
    path = pathlib.Path(asset.file_path)
    if path.exists():
        return

    # Walk asset → script → signal to find the Drive file_id.
    factory = _session_factory()
    async with factory() as session:
        if not asset.script_id:
            raise FileNotFoundError(
                f"publisher.ensure_local_file: {asset.file_path} missing "
                f"and asset has no script_id to resolve Drive source"
            )
        cs = await session.get(ContentScript, asset.script_id)
        if not cs or not cs.signal_id:
            raise FileNotFoundError(
                f"publisher.ensure_local_file: {asset.file_path} missing "
                f"and can't resolve signal"
            )
        sig = await session.get(Signal, cs.signal_id)
        if not sig or sig.source != "drive" or not sig.source_ref:
            raise FileNotFoundError(
                f"publisher.ensure_local_file: {asset.file_path} missing "
                f"and source is not Drive (source={getattr(sig, 'source', None)})"
            )
        drive_file_id = sig.source_ref

    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = await google_drive.download_file(drive_file_id, path)
    log.info(
        "publisher.jit_downloaded",
        asset_id=asset.id,
        file_id=drive_file_id,
        path=str(path),
        bytes=bytes_written,
    )


async def _resolve_drive_filename_for_asset(session, asset_id: str | None) -> str | None:
    """Walk asset → script → signal to find the original Drive filename, if any."""
    if not asset_id:
        return None
    from glitch_signal.db.models import ContentScript, Signal
    asset = await session.get(VideoAsset, asset_id)
    if not asset or not asset.script_id:
        return None
    cs = await session.get(ContentScript, asset.script_id)
    if not cs or not cs.signal_id:
        return None
    sig = await session.get(Signal, cs.signal_id)
    if not sig or sig.source != "drive":
        return None
    return sig.summary.replace("Drive clip: ", "", 1)


async def _publish_to_platform(
    platform: str,
    file_path: str,
    script_id: str,
    brand_id: str | None = None,
    attempts: int = 1,
) -> tuple[str, str | None]:
    if settings().is_dry_run:
        log.info("publisher.dry_run", platform=platform, file_path=file_path, brand_id=brand_id)
        return f"dry-run-{uuid.uuid4().hex[:8]}", None

    if platform == "youtube_shorts":
        from glitch_signal.platforms.youtube import upload_short
        return await upload_short(file_path, script_id, brand_id=brand_id)

    if platform == "tiktok":
        from glitch_signal.platforms.tiktok import publish as tiktok_publish
        return await tiktok_publish(file_path, script_id, brand_id=brand_id)

    if platform.startswith("zernio_"):
        from glitch_signal.platforms.zernio import publish as zernio_publish
        return await zernio_publish(platform, file_path, script_id, brand_id=brand_id)

    if platform.startswith("upload_post_"):
        from glitch_signal.platforms.upload_post import publish as upload_post_publish
        return await upload_post_publish(
            platform, file_path, script_id, brand_id=brand_id, attempts=attempts
        )

    if platform.startswith("buffer_"):
        from glitch_signal.platforms.buffer import publish as buffer_publish
        return await buffer_publish(
            platform, file_path, script_id, brand_id=brand_id, attempts=attempts
        )

    if platform == "twitter":
        from glitch_signal.platforms.twitter import post_video
        return await post_video(file_path, script_id)

    if platform == "instagram_reels":
        from glitch_signal.platforms.instagram import post_reel
        return await post_reel(file_path, script_id)

    raise ValueError(f"Unknown platform: {platform!r}")


async def _handle_failure(scheduled_post_id: str, error: str) -> None:
    factory = _session_factory()
    async with factory() as session:
        sp = await session.get(ScheduledPost, scheduled_post_id)
        if not sp:
            return

        sp.last_error = error[:1000]
        s = settings()
        now = datetime.now(UTC).replace(tzinfo=None)

        if sp.attempts == 1:
            delay_ms = s.publish_retry_1_ms
        elif sp.attempts == 2:
            delay_ms = s.publish_retry_2_ms
        else:
            sp.status = "failed"
            session.add(sp)
            await session.commit()
            return

        sp.status = "queued"
        sp.scheduled_for = now + timedelta(milliseconds=delay_ms)
        session.add(sp)
        await session.commit()


async def _mark_failed(session, sp: ScheduledPost, reason: str) -> None:
    sp.status = "failed"
    sp.last_error = reason
    session.add(sp)
    await session.commit()
