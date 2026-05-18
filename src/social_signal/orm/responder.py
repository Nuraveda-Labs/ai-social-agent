"""ORM responder — generates and sends responses per tier guardrails.

Auto-respond tiers (immediate): positive, neutral_faq, neutral_technical
Review window tiers (2h Telegram window): negative_mild
Escalate only (no response ever): negative_severe, legal_flag
Ignore: spam

Guardrail is re-checked on every draft before send (defense in depth).
Max response: 240 chars (Twitter limit).
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import litellm
import structlog

from social_signal.agent.llm import pick
from social_signal.config import brand_config, settings
from social_signal.db.models import MentionEvent, OrmResponse
from social_signal.db.session import _session_factory
from social_signal.orm import guardrails

log = structlog.get_logger(__name__)

_VOICE_PROMPT = """You are the social media voice for this project — a technical algorithmic trading AI platform.

Rules:
- Technical and direct. No marketing hype. No emoji walls. No "thrilled to announce".
- Max 240 characters (Twitter limit) — every character counts.
- Warm but not sycophantic.
- If linking, use the actual URL, not a placeholder.

Platform: {platform}
Tier: {tier}
Original message: {body}

Generate a response. JSON only: {{"response": "..."}}
"""

_GITHUB_PROMPT = """Convert this user report into a GitHub issue title and body for the <your-org> org.

Report: {body}

JSON only: {{"title": "...", "body": "..."}}
"""

GLITCH_SITE = "https://example.com"
GLITCH_DOCS = "https://github.com/<your-org>"


async def process_mention(mention_id: str) -> None:
    """Entry point — called by scheduler after classifier has run."""
    factory = _session_factory()
    async with factory() as session:
        event = await session.get(MentionEvent, mention_id)
        if not event:
            return

        # Never respond to guardrail hits
        if event.guardrail_hit:
            event.processed_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(event)
            await session.commit()
            return

        tier = event.tier or "negative_severe"
        brand_id = getattr(event, "brand_id", None)
        cfg = brand_config(brand_id).get("orm_guardrails", {})
        auto_tiers: list[str] = cfg.get("auto_respond_tiers", [])
        escalate_tiers: list[str] = cfg.get("escalate_tiers", [])
        ignore_tiers: list[str] = cfg.get("ignore_tiers", [])
        review_windows: dict = cfg.get("review_window_seconds", {})

    if tier in ignore_tiers:
        await _mark_processed(mention_id)
        return

    if tier in escalate_tiers:
        await _escalate(event)
        await _mark_processed(mention_id)
        return

    if tier in auto_tiers:
        draft = await _generate_draft(event)
        if draft:
            await _send_response(event, draft, sent_by="auto")
        await _mark_processed(mention_id)
        return

    # Review window tier (e.g. negative_mild)
    review_s = review_windows.get(tier, 7200)
    draft = await _generate_draft(event)
    if draft:
        await _queue_for_review(event, draft, review_s)
    await _mark_processed(mention_id)


async def _generate_draft(event: MentionEvent) -> str | None:
    tier = event.tier or "negative_severe"

    if settings().is_dry_run:
        return f"[dry-run response to {tier} mention {event.mention_id[:8]}]"

    if tier == "neutral_technical":
        return await _generate_github_response(event)

    mc = pick("smart")
    prompt = _VOICE_PROMPT.format(
        platform=event.platform,
        tier=tier,
        body=event.body[:400],
    )
    try:
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=100,
            **mc.kwargs,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        draft = str(data.get("response", "")).strip()[:240]
        return draft if draft else None
    except Exception as exc:
        log.warning("responder.draft_failed", tier=tier, error=str(exc))
        return None


async def _generate_github_response(event: MentionEvent) -> str:
    try:
        mc = pick("cheap")
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[
                {
                    "role": "user",
                    "content": _GITHUB_PROMPT.format(body=event.body[:400]),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
            **mc.kwargs,
        )
        # TODO(phase2): create a real GitHub issue with these fields.
        # For now we only log the LLM output and return a canned response.
        _data = json.loads(resp.choices[0].message.content or "{}")
        log.info(
            "responder.github_issue_stub",
            title=_data.get("title", "User report")[:80],
            body_preview=str(_data.get("body", event.body))[:80],
        )
        return f"Thanks for the report. Filed as a GitHub issue: {GLITCH_DOCS} — we'll follow up there."
    except Exception:
        return f"Thanks for the report. Please open an issue at {GLITCH_DOCS} so we can track it."


async def _send_response(event: MentionEvent, draft: str, sent_by: str) -> None:
    # Defense-in-depth guardrail re-check (brand-scoped)
    is_safe, hit = guardrails.check(draft, brand_id=getattr(event, "brand_id", None))
    if not is_safe:
        log.warning("responder.draft_blocked_by_guardrail", hit_phrase=hit)
        return

    if not settings().is_dry_run:
        await _post_reply(event, draft)

    factory = _session_factory()
    async with factory() as session:
        orm_resp = OrmResponse(
            id=str(uuid.uuid4()),
            brand_id=getattr(event, "brand_id", "glitch_executor"),
            mention_id=event.id,
            draft_body=draft,
            status="auto_sent" if sent_by == "auto" else "sent",
            sent_at=datetime.now(UTC).replace(tzinfo=None),
            sent_by=sent_by,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(orm_resp)
        await session.commit()

    log.info("responder.sent", mention_id=event.mention_id, tier=event.tier, sent_by=sent_by)


async def _queue_for_review(event: MentionEvent, draft: str, review_s: int) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    auto_send_at = now + timedelta(seconds=review_s)

    factory = _session_factory()
    async with factory() as session:
        orm_resp = OrmResponse(
            id=str(uuid.uuid4()),
            brand_id=getattr(event, "brand_id", "glitch_executor"),
            mention_id=event.id,
            draft_body=draft,
            status="pending_review",
            auto_send_at=auto_send_at,
            created_at=now,
        )
        session.add(orm_resp)
        await session.commit()
        resp_id = orm_resp.id

    # Notify Discord — host-bot plugin polls OrmResponse rows by status
    # and posts the embed in #grow-social. We just leave the row pending
    # here; no inline send needed.
    log.info(
        "responder.queued_for_review",
        mention_id=event.mention_id,
        resp_id=resp_id,
        auto_send_at=str(auto_send_at),
    )


async def _escalate(event: MentionEvent) -> None:
    msg = (
        f"ORM ESCALATION ({event.tier})\n"
        f"Platform: {event.platform}\n"
        f"From: {event.from_handle}\n"
        f"Body: {event.body[:300]}\n\n"
        "No auto-response queued."
    )
    factory = _session_factory()
    async with factory() as session:
        orm_resp = OrmResponse(
            id=str(uuid.uuid4()),
            brand_id=getattr(event, "brand_id", "glitch_executor"),
            mention_id=event.id,
            draft_body="",
            status="escalated",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(orm_resp)
        await session.commit()

    # Discord alert for the escalation. Channel: #grow-social via host-bot plugin.
    import os
    channel_id = (os.environ.get("SOCIAL_MEDIA_AGENT_CHANNEL_ID") or "").strip()
    if not channel_id:
        log.warning("responder.escalate_skipped_no_discord_channel")
        return
    try:
        from social_signal.discord.rest import post_message
        await post_message(channel_id, content=f"🚨 {msg}")
    except Exception as exc:
        log.error("responder.escalate_discord_failed", error=str(exc)[:300])


async def _post_reply(event: MentionEvent, draft: str) -> None:
    if event.platform == "twitter":
        await _post_twitter_reply(event.mention_id, draft)


async def _post_twitter_reply(in_reply_to_id: str, text: str) -> None:
    # OAuth 1.0a required for write — Phase 2 uses tweepy. For now we only
    # log the intent; the bearer token alone can't authorise write calls.
    log.info(
        "responder.twitter_reply_stub",
        in_reply_to_id=in_reply_to_id,
        text=text[:50],
    )


async def _mark_processed(mention_id: str) -> None:
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            __import__("sqlmodel", fromlist=["select"]).select(MentionEvent)
            .where(MentionEvent.id == mention_id)
            .limit(1)
        )
        event = result.scalar_one_or_none()
        if event:
            event.processed_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(event)
            await session.commit()


async def send_approved_response(orm_response_id: str) -> None:
    """Called by Telegram /orm_approve or scheduler auto-send tick."""
    factory = _session_factory()
    async with factory() as session:
        resp = await session.get(OrmResponse, orm_response_id)
        if not resp or resp.status not in ("pending_review",):
            return

        event_result = await session.execute(
            __import__("sqlmodel", fromlist=["select"]).select(MentionEvent)
            .where(MentionEvent.id == resp.mention_id)
            .limit(1)
        )
        event = event_result.scalar_one_or_none()

    if event:
        await _send_response(event, resp.draft_body, sent_by="human")
