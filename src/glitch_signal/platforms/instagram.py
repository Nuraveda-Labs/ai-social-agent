"""Instagram Reels publisher — Phase 2.

Auth: Meta Graph API with instagram_content_publish + instagram_basic scopes.
NOTE: Separate from the existing Meta Ads token in ai_marketing_stack-ads-agent.
      Requires a different app with content_publish permission approved by Meta.

Two-step publish:
  1. POST /{ig-user-id}/media  (create container, poll until FINISHED)
  2. POST /{ig-user-id}/media_publish  (publish)
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def post_reel(file_path: str, script_id: str) -> tuple[str, str | None]:
    """Publish a Reel. Returns (media_id, post_url). Phase 2."""
    raise NotImplementedError(
        "Instagram publisher lands in Phase 2. "
        "Requires Meta Graph API instagram_content_publish permission (separate from Ads token)."
    )
