"""Cron entry point: fire one due sheet-driven post if pacing allows.

Designed to be run by a cron entry every ~5 minutes. The pacing rules
inside reader.fetch_next_due (min interval per brand+platform + daily
cap) mean most ticks return None and exit cleanly without posting; a
post only goes out when the schedule + pacing converge.

One row per tick. Idempotent — the row's status flips to posted/failed
on completion, so a tick fires while the previous one is still rendering
will pick a *different* row (or None if pacing blocks).

Exit codes:
  0  posted a row, OR no row was due (both are fine)
  1  hard failure (sheet unreachable, posting infrastructure broken)
"""
from __future__ import annotations

import asyncio
import sys

import structlog


async def _tick() -> int:
    log = structlog.get_logger("sheet_posting_tick")
    try:
        from glitch_signal.sheet_posting.poster import post_one
        from glitch_signal.sheet_posting.reader import fetch_next_due
    except Exception as exc:
        log.error("tick.import_failed", error=str(exc)[:300])
        return 1

    try:
        row = await fetch_next_due()
    except Exception as exc:
        log.error("tick.fetch_failed", error=str(exc)[:300])
        return 1

    if not row:
        log.info("tick.idle", reason="no row due (pacing or schedule)")
        return 0

    log.info(
        "tick.firing",
        row_id=row.id, brand=row.brand_id, platform=row.platform,
        ct=row.content_type,
    )
    try:
        ok, msg = await post_one(row)
        log.info("tick.done", row_id=row.id, ok=ok, msg=msg[:200])
        return 0
    except Exception as exc:
        log.error("tick.post_failed", row_id=row.id, error=str(exc)[:300])
        # Soft fail — the row's status will be flipped by post_one's own
        # error handling; don't crash the cron job.
        return 0


def main() -> int:
    return asyncio.run(_tick())


if __name__ == "__main__":
    sys.exit(main())
