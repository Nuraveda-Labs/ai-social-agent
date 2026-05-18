"""Reconcile sheet rows whose platform_post_id still carries a `request:xxx`
placeholder — i.e. Upload-Post accepted the post into a background worker
but hadn't finalized the real platform post id when we wrote the row back.

Runs on a scheduler tick every ~10 min. For each such row, calls Upload-Post's
get_status(request_id=...); if the status is completed, writes the real
platform_post_id + post_url back to the sheet. Rows where the vendor is
still processing are left alone and retried on the next tick.
"""
from __future__ import annotations

import asyncio

import structlog

from glitch_signal.config import settings
from glitch_signal.integrations.google_sheets import update_row_by_key
from glitch_signal.sheet_posting.reader import SHEET_COLUMNS, fetch_all_rows

log = structlog.get_logger(__name__)


async def reconcile_pending() -> dict:
    """One reconciliation pass. Returns a summary dict for logging."""
    s = settings()
    if not s.glitch_posts_sheet_id or not s.upload_post_api_key:
        return {"checked": 0, "reconciled": 0}

    import upload_post

    client = upload_post.UploadPostClient(api_key=s.upload_post_api_key)
    rows = await fetch_all_rows()

    pending = [
        r for r in rows
        if r.status == "posted" and r.platform_post_id.startswith("request:")
    ]
    if not pending:
        return {"checked": 0, "reconciled": 0}

    reconciled = 0
    for r in pending:
        req_id = r.platform_post_id.split(":", 1)[1]
        try:
            st = await asyncio.to_thread(client.get_status, request_id=req_id)
        except Exception as exc:
            log.warning("reconciler.get_status_failed", row_id=r.id, error=str(exc)[:200])
            continue

        vendor_status = (st or {}).get("status")
        results = (st or {}).get("results") or []
        result = results[0] if results else {}
        real_pid = result.get("platform_post_id")
        url = result.get("post_url") or result.get("url")

        if not real_pid:
            # Still processing, or the vendor never produced a post id.
            log.debug(
                "reconciler.still_pending",
                row_id=r.id, request_id=req_id, vendor_status=vendor_status,
            )
            continue

        try:
            await update_row_by_key(
                sheet_id=s.glitch_posts_sheet_id,
                worksheet=r.worksheet or s.glitch_posts_worksheet,
                columns=SHEET_COLUMNS,
                key_column="id",
                key_value=r.id,
                updates={
                    "platform_post_id": str(real_pid),
                    "post_url": url or "",
                },
            )
            reconciled += 1
            log.info(
                "reconciler.row_reconciled",
                row_id=r.id, platform=r.platform, platform_post_id=real_pid,
            )
        except Exception as exc:
            log.warning("reconciler.sheet_update_failed", row_id=r.id, error=str(exc)[:200])

    return {"checked": len(pending), "reconciled": reconciled}
