"""Brand-task progress tracker — thin layer over google_sheets.

Centralises the schema and the row-locator convention so the agent's
nodes (drive_scout → caption_writer → publisher) stay decoupled from
the underlying Sheets API.

Schema:

  video_name | drive_link | product | variant_group | geo
  caption    | status     | scheduled_for | posted_at | tiktok_url | notes

Row key: `video_name` (the raw Drive filename). One row per file — new
rows are appended by drive_scout; later nodes do partial updates.

Brand opts in by setting
  tasks.video_uploader.outputs.google_sheet.{sheet_id, worksheet}
in the brand config. A brand with no sheet configured is a no-op for
every function here (sheet tracking is optional).
"""
from __future__ import annotations

import structlog

from glitch_signal.config import brand_config
from glitch_signal.integrations import google_sheets as gs

log = structlog.get_logger(__name__)


TRACKER_COLUMNS = [
    "video_name", "drive_link", "product", "variant_group", "geo",
    "caption", "status", "scheduled_for", "posted_at", "tiktok_url", "notes",
]

_KEY_COLUMN = "video_name"


def sheet_target(brand_id: str) -> tuple[str, str] | None:
    """Return (sheet_id, worksheet) for the brand, or None if not configured."""
    try:
        cfg = brand_config(brand_id)
    except Exception:
        return None
    task = (cfg.get("tasks") or {}).get("video_uploader") or {}
    out = (task.get("outputs") or {}).get("google_sheet") or {}
    sheet_id = out.get("sheet_id")
    if not sheet_id:
        return None
    return sheet_id, out.get("worksheet", "Sheet1")


async def ensure_header(brand_id: str) -> bool:
    """Write the column headers if absent/stale. No-op when no sheet configured."""
    target = sheet_target(brand_id)
    if not target:
        return False
    sheet_id, worksheet = target
    try:
        await gs.ensure_header(sheet_id, worksheet, TRACKER_COLUMNS)
        return True
    except Exception as exc:
        log.warning(
            "sheet_tracker.header_failed",
            brand_id=brand_id, error=str(exc)[:200],
        )
        return False


async def append_new_video(
    brand_id: str,
    video_name: str,
    drive_file_id: str,
    *,
    product: str | None = None,
    variant_group: str | None = None,
    geo: str | None = None,
) -> None:
    """Append a fresh `queued` row for a video. Never raises."""
    target = sheet_target(brand_id)
    if not target:
        return
    sheet_id, worksheet = target
    row = {
        "video_name":    video_name,
        "drive_link":    f"https://drive.google.com/file/d/{drive_file_id}/view",
        "product":       product or "",
        "variant_group": variant_group or "",
        "geo":           geo or "",
        "caption":       "",
        "status":        "queued",
        "scheduled_for": "",
        "posted_at":     "",
        "tiktok_url":    "",
        "notes":         "",
    }
    try:
        await gs.append_row(sheet_id, worksheet, TRACKER_COLUMNS, row)
    except Exception as exc:
        log.warning(
            "sheet_tracker.append_failed",
            brand_id=brand_id, video_name=video_name, error=str(exc)[:200],
        )


async def read_by_video_name(
    brand_id: str, video_name: str
) -> dict[str, str] | None:
    """Return the tracker row for `video_name` as a dict, or None if not found.

    Used by caption_writer to pick up human-written captions from the sheet
    before falling back to LLM generation.
    """
    target = sheet_target(brand_id)
    if not target:
        return None
    sheet_id, worksheet = target
    try:
        return await gs.read_row_by_key(
            sheet_id, worksheet, TRACKER_COLUMNS,
            key_column=_KEY_COLUMN, key_value=video_name,
        )
    except Exception as exc:
        log.warning(
            "sheet_tracker.read_failed",
            brand_id=brand_id, video_name=video_name, error=str(exc)[:200],
        )
        return None


async def update_by_video_name(
    brand_id: str, video_name: str, updates: dict
) -> bool:
    """Partial update keyed on video_name. Returns False if no row found or no sheet configured."""
    target = sheet_target(brand_id)
    if not target:
        return False
    sheet_id, worksheet = target
    try:
        return await gs.update_row_by_key(
            sheet_id, worksheet, TRACKER_COLUMNS,
            key_column=_KEY_COLUMN, key_value=video_name,
            updates=updates,
        )
    except Exception as exc:
        log.warning(
            "sheet_tracker.update_failed",
            brand_id=brand_id, video_name=video_name, error=str(exc)[:200],
        )
        return False
