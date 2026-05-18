"""Twitter / X video publisher — Phase 2.

Auth: OAuth 1.0a via tweepy (Basic tier $100/mo required).
Upload: chunked media upload for videos > 5MB.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def post_video(file_path: str, script_id: str) -> tuple[str, str | None]:
    """Upload video and post tweet. Returns (tweet_id, tweet_url). Phase 2."""
    raise NotImplementedError(
        "Twitter publisher lands in Phase 2. "
        "Requires Basic tier API subscription ($100/mo) and OAuth 1.0a credentials."
    )
