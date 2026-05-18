"""Inbound webhook dispatcher for Upload-Post events.

Upload-Post POSTs to `/webhooks/upload_post/<secret>` with a JSON body
shaped like (observed from their docs + SDK echoes):

    {
      "event": "upload_completed",          # or "event_type"
      "job_id": "<request_id>",              # sometimes "request_id"
      "user": "MyBrand",
      "platform": "tiktok",                  # or nested inside `results`
      "results": [
        {"platform": "tiktok", "success": true,
         "platform_post_id": "7629...", "post_url": "https://..."}
      ]
      ...
    }

This module is deliberately platform-agnostic about framework/transport —
server.py verifies the URL secret, parses JSON, and hands the dict here.
That keeps the handlers trivially testable (no FastAPI needed).

Event handlers supported:
  - upload_completed             → finalize ScheduledPost + write PublishedPost
  - social_account_connected     → log
  - social_account_disconnected  → mark PlatformAuth status=revoked
  - social_account_reauth_required → mark PlatformAuth status=needs_reauth

Unknown events are logged and ignored (forward-compatible with new UP events).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from glitch_signal.db.models import PlatformAuth, PublishedPost, ScheduledPost
from glitch_signal.db.session import _session_factory
from glitch_signal.platforms.upload_post import (
    _PLATFORM_MAP,
    extract_post_from_event,
)

log = structlog.get_logger(__name__)


# Upload-Post's `platform` field uses their canonical names (e.g. "tiktok").
# Our ScheduledPost.platform stores the routing key ("upload_post_tiktok").
# Invert _PLATFORM_MAP so we can go UP → ours.
_UP_TO_LOCAL_PLATFORM = {v: k for k, v in _PLATFORM_MAP.items()}


async def dispatch(event: dict) -> dict:
    """Route a decoded webhook payload to the matching handler.

    Returns a small dict suitable for the HTTP response: {"ok": bool,
    "event": str, "handled": bool, ...diagnostic fields}. Never raises —
    the caller returns 200 regardless so Upload-Post doesn't retry.
    """
    event_type = (
        event.get("event")
        or event.get("event_type")
        or event.get("type")
        or ""
    ).strip().lower()

    if not event_type:
        log.warning("upload_post.webhook.missing_event_type", keys=list(event.keys()))
        return {"ok": False, "handled": False, "reason": "missing event type"}

    if event_type in ("upload_completed", "upload.completed", "upload_complete"):
        return await _handle_upload_completed(event)
    if event_type in ("social_account_connected", "account.connected"):
        return await _handle_account_connected(event)
    if event_type in ("social_account_disconnected", "account.disconnected"):
        return await _handle_account_disconnected(event)
    if event_type in (
        "social_account_reauth_required",
        "account.reauth_required",
        "reauth_required",
    ):
        return await _handle_reauth_required(event)

    log.info("upload_post.webhook.unhandled_event", event_type=event_type)
    return {"ok": True, "handled": False, "event": event_type}


# ---------------------------------------------------------------------------
# upload_completed
# ---------------------------------------------------------------------------

async def _handle_upload_completed(event: dict) -> dict:
    """Correlate the event back to a ScheduledPost and finalize it.

    Lookup strategy: the event always carries the request_id (sometimes
    called job_id). We match `ScheduledPost.vendor_request_id == request_id`.
    If no row matches, the post was published outside our system (or we
    already processed this event) — log and return ok.
    """
    request_id = (
        event.get("job_id")
        or event.get("request_id")
        or event.get("requestId")
    )
    if not request_id:
        log.warning("upload_post.webhook.missing_request_id", event_keys=list(event.keys()))
        return {"ok": False, "handled": False, "reason": "missing request_id"}

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.vendor_request_id == request_id
            )
        )
        sp = result.scalar_one_or_none()
        if not sp:
            log.info(
                "upload_post.webhook.no_matching_scheduled_post",
                request_id=request_id,
            )
            return {"ok": True, "handled": False, "request_id": request_id}

        # Idempotency — if a PublishedPost row already exists for this sp
        # (webhook fired twice, or reconciliation sweep raced the webhook),
        # no-op.
        existing = (await session.execute(
            select(PublishedPost).where(
                PublishedPost.scheduled_post_id == sp.id
            )
        )).scalar_one_or_none()
        if existing:
            if sp.status != "done":
                sp.status = "done"
                session.add(sp)
                await session.commit()
            log.info(
                "upload_post.webhook.already_published",
                scheduled_post_id=sp.id,
                platform_post_id=existing.platform_post_id,
            )
            return {
                "ok": True, "handled": True, "duplicate": True,
                "scheduled_post_id": sp.id,
            }

        # Figure out which platform key to look inside `results` for.
        # sp.platform is e.g. "upload_post_tiktok" — we need the UP-side
        # name "tiktok".
        target_platform = _PLATFORM_MAP.get(sp.platform)
        if not target_platform:
            # Caller may have stored a Zernio or direct platform here in
            # the future; for now this module only handles upload_post_*.
            log.warning(
                "upload_post.webhook.non_upload_post_platform",
                scheduled_post_id=sp.id,
                platform=sp.platform,
            )
            return {"ok": False, "handled": False, "reason": "platform mismatch"}

        ppid, url, err = extract_post_from_event(event, target_platform)

        if err and not ppid:
            sp.status = "failed"
            sp.last_error = str(err)[:1000]
            session.add(sp)
            await session.commit()
            log.error(
                "upload_post.webhook.publish_failed",
                scheduled_post_id=sp.id,
                request_id=request_id,
                error=str(err)[:200],
            )
            return {
                "ok": True, "handled": True, "scheduled_post_id": sp.id,
                "status": "failed", "error": str(err)[:200],
            }

        if not ppid and not url:
            # Event arrived but with no actionable payload. Leave the sp
            # in awaiting_webhook so the reconciliation sweep can poll
            # get_status later.
            log.warning(
                "upload_post.webhook.empty_result",
                scheduled_post_id=sp.id,
                request_id=request_id,
            )
            return {
                "ok": True, "handled": False,
                "scheduled_post_id": sp.id,
                "reason": "no platform_post_id or url in event",
            }

        # Happy path — write PublishedPost + mark sp done.
        pub = PublishedPost(
            id=str(uuid.uuid4()),
            brand_id=sp.brand_id,
            scheduled_post_id=sp.id,
            platform=sp.platform,
            platform_post_id=ppid or request_id,
            platform_url=url,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        sp.status = "done"
        session.add(pub)
        session.add(sp)
        await session.commit()

        log.info(
            "upload_post.webhook.published",
            scheduled_post_id=sp.id,
            platform_post_id=pub.platform_post_id,
            url=pub.platform_url,
        )
        return {
            "ok": True, "handled": True,
            "scheduled_post_id": sp.id,
            "platform_post_id": pub.platform_post_id,
        }


# ---------------------------------------------------------------------------
# Account lifecycle events — drive the platform_auth.status field so the
# agent can surface reauth needs before posts silently start failing.
# ---------------------------------------------------------------------------

async def _handle_account_connected(event: dict) -> dict:
    user = event.get("user") or event.get("username")
    platform = event.get("platform")
    log.info("upload_post.webhook.account_connected", user=user, platform=platform)
    return {"ok": True, "handled": True, "event": "account_connected"}


async def _handle_account_disconnected(event: dict) -> dict:
    return await _set_platform_auth_status(event, new_status="revoked")


async def _handle_reauth_required(event: dict) -> dict:
    return await _set_platform_auth_status(event, new_status="needs_reauth")


async def _set_platform_auth_status(event: dict, *, new_status: str) -> dict:
    """Update PlatformAuth.status for whichever brand account the event concerns.

    Event carries `user` (Upload-Post profile name) and `platform`. We
    don't store UP's profile name on PlatformAuth, so match on platform
    + account_identifier when available; otherwise log a best-effort
    entry without touching the DB. Future work: persist a mapping from
    UP profile name → our brand_id.
    """
    user = event.get("user") or event.get("username")
    platform_up = event.get("platform")
    account_id = event.get("account_id") or event.get("account_identifier")
    reason = event.get("reason") or event.get("message")

    if not platform_up:
        log.warning(
            "upload_post.webhook.account_event_missing_platform",
            new_status=new_status,
            user=user,
        )
        return {"ok": False, "handled": False, "reason": "missing platform"}

    factory = _session_factory()
    async with factory() as session:
        query = select(PlatformAuth).where(PlatformAuth.platform == platform_up)
        if account_id:
            query = query.where(PlatformAuth.account_identifier == account_id)
        result = await session.execute(query)
        rows = result.scalars().all()

        if not rows:
            log.info(
                "upload_post.webhook.no_matching_platform_auth",
                platform=platform_up,
                account_id=account_id,
                user=user,
                new_status=new_status,
            )
            return {"ok": True, "handled": False, "reason": "no matching PlatformAuth"}

        updated_ids: list[str] = []
        for row in rows:
            row.status = new_status
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(row)
            updated_ids.append(row.id)
        await session.commit()

    log.warning(
        "upload_post.webhook.account_status_changed",
        platform=platform_up,
        account_id=account_id,
        user=user,
        new_status=new_status,
        reason=reason,
        updated_ids=updated_ids,
    )
    return {
        "ok": True, "handled": True,
        "new_status": new_status,
        "updated_ids": updated_ids,
    }
