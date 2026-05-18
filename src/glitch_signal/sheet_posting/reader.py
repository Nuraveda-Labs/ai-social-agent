"""Google Sheet → queue reader for scheduled text posts.

One row per post. The operator maintains the sheet (edit bodies, mark rows
skip, change status back to queued, etc.). The scheduler tick reads the
sheet on every run, picks the next due row respecting per-brand pacing,
hands it to the poster, and writes the result back.

Sheet schema (row 1 = header):
    id                  UUID. Filled in by setup script; used as the key
                        for update_row_by_key on completion.
    brand_id            glitch_executor | glitch_founder
    platform            upload_post_x | upload_post_linkedin
    body                post text (X ≤ 280 chars; LinkedIn ≤ 2800).
                        For quote_card + carousel rows, this is also the
                        social-media caption shown alongside the image/PDF.
    content_type        text | quote_card | carousel
                        text         → plain text post (current default)
                        quote_card   → gpt-image-2 designed image, body as
                                       caption, uploaded as single image
                        carousel     → LinkedIn PDF carousel (body becomes
                                       description, slides are LLM-split
                                       from body and rendered via gpt-image-2)
    status              queued | posted | failed | skip | draft
    scheduled_for       ISO datetime. Empty = "as soon as pacing allows".
    posted_at           Filled by poster on success.
    post_url            Filled by poster on success.
    platform_post_id    Filled by poster on success.
    notes               Free-form operator column; never read by code.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from glitch_signal.config import settings

log = structlog.get_logger(__name__)

SHEET_COLUMNS: list[str] = [
    "id",
    "brand_id",
    "platform",
    "body",
    "content_type",
    "status",
    "scheduled_for",
    "posted_at",
    "post_url",
    "platform_post_id",
    "notes",
]


@dataclass
class QueuedPost:
    """One decoded row from the posts sheet."""
    id: str
    brand_id: str
    platform: str
    body: str
    content_type: str
    status: str
    scheduled_for: datetime | None
    posted_at: datetime | None
    post_url: str
    platform_post_id: str
    notes: str
    # Worksheet tab this row was read from. Used by poster + reconciler so
    # status updates land in the right tab. Defaults to "queue" for legacy
    # single-tab callers.
    worksheet: str = "queue"

    @classmethod
    def from_row(cls, row: dict[str, str], *, worksheet: str = "queue") -> QueuedPost:
        # content_type defaults: "carousel" for LinkedIn (legacy behaviour
        # before the column existed), "text" for everything else.
        platform = row.get("platform", "").strip()
        default_ct = "carousel" if platform == "upload_post_linkedin" else "text"
        content_type = (row.get("content_type") or "").strip().lower() or default_ct
        return cls(
            id=row.get("id", "").strip(),
            brand_id=row.get("brand_id", "").strip(),
            platform=platform,
            body=row.get("body", ""),
            content_type=content_type,
            status=(row.get("status") or "draft").strip().lower(),
            scheduled_for=_parse_iso(row.get("scheduled_for", "")),
            posted_at=_parse_iso(row.get("posted_at", "")),
            post_url=row.get("post_url", "").strip(),
            platform_post_id=row.get("platform_post_id", "").strip(),
            notes=row.get("notes", ""),
            worksheet=worksheet,
        )


def _parse_iso(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        # Sheets serializes datetimes in many shapes; handle ISO-ish.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Read / selection
# ---------------------------------------------------------------------------

def _worksheet_list() -> list[str]:
    """Worksheets to read on every fetch.

    Order matters only for stable IDs across reads; both tabs are scanned.
    Falls back to the legacy single-tab name when the new tabs aren't set.
    """
    s = settings()
    if s.glitch_posts_brand_worksheet or s.glitch_posts_founder_worksheet:
        names: list[str] = []
        if s.glitch_posts_brand_worksheet:
            names.append(s.glitch_posts_brand_worksheet)
        if s.glitch_posts_founder_worksheet:
            names.append(s.glitch_posts_founder_worksheet)
        return names
    return [s.glitch_posts_worksheet or "queue"]


async def fetch_all_rows() -> list[QueuedPost]:
    """Read every row of every configured worksheet."""
    s = settings()
    if not s.glitch_posts_sheet_id:
        return []
    out: list[QueuedPost] = []
    for ws in _worksheet_list():
        try:
            raw = await asyncio.to_thread(_read_rows_sync, s.glitch_posts_sheet_id, ws)
        except Exception:
            continue
        for r in raw:
            out.append(QueuedPost.from_row(r, worksheet=ws))
    return out


async def fetch_next_due(now: datetime | None = None) -> QueuedPost | None:
    """Return the single next post to publish, or None if pacing blocks all.

    Selection rules (in order):
      1. status == "queued"
      2. scheduled_for is empty OR scheduled_for <= now
      3. (brand_id, platform) has not posted within
         glitch_posts_min_interval_minutes
      4. (brand_id, platform) hasn't hit glitch_posts_daily_cap today (UTC)
      5. Tie-break by scheduled_for asc, then row order (sheet position).
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    all_rows = await fetch_all_rows()
    if not all_rows:
        return None

    s = settings()
    interval = s.glitch_posts_min_interval_minutes * 60  # seconds
    cap = s.glitch_posts_daily_cap

    # Last-posted + today-count per (brand, platform)
    last_posted: dict[tuple[str, str], datetime] = {}
    today_counts: dict[tuple[str, str], int] = {}
    today_key = now.date()
    for r in all_rows:
        if r.status == "posted" and r.posted_at:
            k = (r.brand_id, r.platform)
            prev = last_posted.get(k)
            if prev is None or r.posted_at > prev:
                last_posted[k] = r.posted_at
            if r.posted_at.date() == today_key:
                today_counts[k] = today_counts.get(k, 0) + 1

    def _due(r: QueuedPost) -> bool:
        if r.status != "queued":
            return False
        if not r.id or not r.brand_id or not r.platform or not r.body.strip():
            return False
        if r.scheduled_for and r.scheduled_for > now:
            return False
        k = (r.brand_id, r.platform)
        last = last_posted.get(k)
        if last and (now - last).total_seconds() < interval:
            return False
        if today_counts.get(k, 0) >= cap:
            return False
        return True

    candidates = [r for r in all_rows if _due(r)]
    if not candidates:
        return None

    candidates.sort(
        key=lambda r: (r.scheduled_for or datetime.min, r.id)
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Sync workers
# ---------------------------------------------------------------------------

def _read_rows_sync(sheet_id: str, worksheet: str) -> list[dict[str, str]]:
    from glitch_signal.integrations.google_sheets import _service

    svc = _service()
    rng = f"'{worksheet}'!A:K"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng,
    ).execute()
    values: list[list[Any]] = resp.get("values", [])
    if not values:
        return []

    header = [str(c).strip() for c in values[0]]
    out: list[dict[str, str]] = []
    for row in values[1:]:
        record: dict[str, str] = {}
        for i, col in enumerate(header):
            record[col] = str(row[i]) if i < len(row) else ""
        out.append(record)
    return out
