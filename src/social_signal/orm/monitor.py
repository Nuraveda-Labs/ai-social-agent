"""ORM monitor — polls social platforms for inbound mentions.

Phase 1: Twitter/X mentions only (every 5 minutes via scheduler tick).
Phase 2: YouTube comments + Instagram DMs added.

Writes MentionEvent rows with unique mention_id (dedup constraint prevents
duplicate rows on repeated polls).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from social_signal.config import settings
from social_signal.db.models import MentionEvent
from social_signal.db.session import _session_factory
from social_signal.orm import classifier, guardrails

log = structlog.get_logger(__name__)

TWITTER_API = "https://api.twitter.com/2"


async def poll_all() -> int:
    """Poll all configured platforms. Returns total new mentions found."""
    total = 0
    total += await _poll_twitter()
    # Phase 2: total += await _poll_youtube()
    # Phase 2: total += await _poll_instagram()
    return total


# ---------------------------------------------------------------------------
# Twitter / X
# ---------------------------------------------------------------------------

async def _poll_twitter() -> int:
    bearer = settings().twitter_bearer_token
    if not bearer:
        return 0

    # Get our own user ID first (cached via simple lookup)
    user_id = await _get_twitter_user_id(bearer)
    if not user_id:
        return 0

    # Fetch recent mentions
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{TWITTER_API}/users/{user_id}/mentions",
            headers={"Authorization": f"Bearer {bearer}"},
            params={
                "tweet.fields": "id,text,author_id,in_reply_to_user_id,created_at",
                "max_results": 100,
            },
        )
        if resp.status_code == 401:
            log.warning("monitor.twitter.auth_failed")
            return 0
        resp.raise_for_status()

    data = resp.json()
    tweets = data.get("data", [])
    if not tweets:
        return 0

    new_count = 0
    for tweet in tweets:
        mention_id = str(tweet.get("id", ""))
        body = str(tweet.get("text", ""))
        author_id = str(tweet.get("author_id", ""))

        if await _already_seen(mention_id):
            continue

        # ORM monitor today targets a single (this project) account per
        # platform, so mentions are bound to the default brand. When multi-
        # account ORM lands, derive brand_id from the receiving account here.
        brand_id = settings().default_brand_id

        is_safe, hit_phrase = guardrails.check(body, brand_id=brand_id)
        tier_data = await classifier.classify(body, "twitter", brand_id=brand_id)

        event = MentionEvent(
            id=str(uuid.uuid4()),
            brand_id=brand_id,
            platform="twitter",
            mention_id=mention_id,
            body=body,
            from_handle=author_id,
            author_id=author_id,
            tier=tier_data["tier"],
            sentiment=tier_data["sentiment"],
            confidence=tier_data["confidence"],
            guardrail_hit=not is_safe,
            received_at=datetime.now(UTC).replace(tzinfo=None),
        )

        try:
            factory = _session_factory()
            async with factory() as session:
                session.add(event)
                await session.commit()
            new_count += 1
        except IntegrityError:
            pass  # duplicate mention_id — already inserted by a concurrent tick

        if not is_safe:
            log.warning(
                "monitor.guardrail_hit",
                mention_id=mention_id,
                hit_phrase=hit_phrase,
            )
            await _alert_discord(
                f"GUARDRAIL HIT on Twitter mention {mention_id}\n"
                f"Phrase: {hit_phrase}\n"
                f"Body: {body[:200]}"
            )

    return new_count


async def _get_twitter_user_id(bearer: str) -> str | None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{TWITTER_API}/users/me",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if not resp.is_success:
            return None
    return str(resp.json().get("data", {}).get("id", ""))


async def _already_seen(mention_id: str) -> bool:
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(MentionEvent).where(MentionEvent.mention_id == mention_id).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _alert_discord(message: str) -> None:
    """Post a guardrail alert into the Discord control-plane channel.
    Replaces the Telegram alert path that was retired 2026-05-01."""
    import os
    channel_id = (os.environ.get("SOCIAL_MEDIA_AGENT_CHANNEL_ID") or "").strip()
    if not channel_id:
        log.warning("monitor.discord_alert_skipped_no_channel")
        return
    try:
        from social_signal.discord.rest import post_message
        await post_message(channel_id, content=f"⚠️ {message}")
    except Exception as exc:
        log.error("monitor.discord_alert_failed", error=str(exc)[:300])
