"""Comment engagement sweeper — read comments on our published posts,
triage them, draft replies in brand voice, queue for Telegram approval.

Flow:
    1. Scheduler tick calls sweep_comments() every N minutes
    2. For each recent PublishedPost (within lookback window):
         a. Upload-Post get_post_comments(user, post_id)
         b. For each comment not yet in comment_reply table:
            - LLM triage (reply_worthy | spam | promo | skip)
            - If reply_worthy: LLM drafts reply in brand voice
            - Write CommentReply row, status=pending_approval
            - Send Telegram preview with Approve/Veto buttons
    3. Telegram callback handler (handlers.py) fires approve/veto →
       reply_to_comment() via Upload-Post

LinkedIn algorithm rewards author-replies 15× more than likes, which makes
this the single highest-leverage engagement lever we have on the platform.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import litellm
import structlog
from sqlmodel import select
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.agent.llm import pick
from glitch_signal.agent.nodes.text_writer import _forbidden_hits
from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import CommentReply, PublishedPost
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

# How far back we look for new comments on any given sweep. Posts older
# than this are considered "past the active engagement window" and we stop
# polling to save API calls.
COMMENT_LOOKBACK_DAYS = 14

# Upload-Post comment-read is free but still an API call per post. Cap
# per-tick so one sweep doesn't spike the API budget.
MAX_POSTS_PER_SWEEP = 20

# Upload-Post's get_post_comments is hardcoded to Instagram only — the
# SDK sends `platform=instagram` on every call regardless of the user's
# actual platform, so passing an X or LinkedIn post_id results in
# "Invalid post_id. The Instagram Graph API requires a numeric media ID."
# and/or 429 rate-limit storms.
#
# Until Upload-Post exposes X / LinkedIn comment endpoints, we restrict
# the sweeper to Instagram-published posts only. X/LinkedIn comments
# need platform-native API integration (deferred — Twitter API v2 mentions
# endpoint for X; LinkedIn doesn't expose arbitrary post comments to
# third-party apps).
COMMENT_PLATFORMS = ("upload_post_instagram",)


async def sweep_comments() -> dict:
    """One sweep pass. Returns a summary dict for logging."""
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=COMMENT_LOOKBACK_DAYS)

    summary = {"posts_checked": 0, "new_comments": 0, "drafts_queued": 0, "skipped": 0}
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PublishedPost)
            .where(PublishedPost.published_at >= cutoff)
            .where(PublishedPost.platform.in_(COMMENT_PLATFORMS))
            .order_by(PublishedPost.published_at.desc())
            .limit(MAX_POSTS_PER_SWEEP)
        )
        posts = result.scalars().all()

    for post in posts:
        try:
            new_count, draft_count, skip_count = await _sweep_one_post(post)
            summary["posts_checked"] += 1
            summary["new_comments"] += new_count
            summary["drafts_queued"] += draft_count
            summary["skipped"] += skip_count
        except Exception as exc:
            log.warning(
                "comments.sweep_one_failed",
                published_post_id=post.id,
                platform=post.platform,
                error=str(exc)[:200],
            )

    if summary["new_comments"] or summary["drafts_queued"]:
        log.info("comments.sweep_summary", **summary)
    return summary


async def _sweep_one_post(post: PublishedPost) -> tuple[int, int, int]:
    """Return (new_comments, drafts_queued, skipped)."""
    cfg = brand_config(post.brand_id)
    platforms_cfg = cfg.get("platforms", {}) or {}
    block = platforms_cfg.get(post.platform) or {}
    user = block.get("user")
    if not user:
        # No Upload-Post user configured for this platform on this brand.
        return 0, 0, 0

    api_key = settings().upload_post_api_key
    if not api_key:
        return 0, 0, 0

    comments = await asyncio.to_thread(
        _fetch_comments, api_key, user, post.platform_post_id
    )
    if not comments:
        return 0, 0, 0

    new_count = 0
    drafted = 0
    skipped = 0

    factory = _session_factory()
    async with factory() as session:
        # Deduplicate: skip comments we already have a row for
        ids = [c.get("id") or c.get("comment_id") for c in comments]
        ids = [i for i in ids if i]
        if ids:
            existing = (await session.execute(
                select(CommentReply.platform_comment_id).where(
                    CommentReply.platform_comment_id.in_(ids)
                )
            )).scalars().all()
        else:
            existing = []
        seen = set(existing)

        for c in comments:
            cid = c.get("id") or c.get("comment_id")
            if not cid or cid in seen:
                continue
            new_count += 1

            text = (c.get("text") or c.get("body") or c.get("message") or "").strip()
            if not text:
                skipped += 1
                continue

            commenter = (
                c.get("author") or {}
            ) if isinstance(c.get("author"), dict) else {}
            handle = commenter.get("handle") or commenter.get("username") or c.get("author_handle")
            name = commenter.get("name") or c.get("author_name")

            # Don't reply to ourselves (our brand account commenting on our own post)
            if handle and user and handle.lower() == str(user).lower():
                skipped += 1
                continue

            try:
                triage = await _triage_comment(text, platform=post.platform)
            except Exception as exc:
                log.warning("comments.triage_failed", error=str(exc)[:200])
                triage = "skip"

            if triage != "reply_worthy":
                # Record anyway so we don't re-triage next sweep
                row = CommentReply(
                    id=str(uuid.uuid4()),
                    brand_id=post.brand_id,
                    platform=post.platform,
                    published_post_id=post.id,
                    platform_post_id=post.platform_post_id,
                    platform_comment_id=cid,
                    commenter_handle=handle,
                    commenter_name=name,
                    comment_text=text,
                    triage_tier=triage,
                    status="ignored",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
                session.add(row)
                skipped += 1
                continue

            try:
                drafted_reply = await _draft_reply(
                    brand_id=post.brand_id,
                    platform=post.platform,
                    original_post=_post_excerpt(post),
                    comment_text=text,
                )
            except Exception as exc:
                log.warning("comments.draft_failed", error=str(exc)[:200])
                drafted_reply = None

            row = CommentReply(
                id=str(uuid.uuid4()),
                brand_id=post.brand_id,
                platform=post.platform,
                published_post_id=post.id,
                platform_post_id=post.platform_post_id,
                platform_comment_id=cid,
                commenter_handle=handle,
                commenter_name=name,
                comment_text=text,
                triage_tier=triage,
                status="pending_approval" if drafted_reply else "failed",
                drafted_reply=drafted_reply,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(row)
            await session.flush()
            if drafted_reply:
                drafted += 1
                await _send_approval_message(row)

        await session.commit()

    return new_count, drafted, skipped


def _fetch_comments(api_key: str, user: str, platform_post_id: str) -> list[dict]:
    """Blocking SDK call → list of comment dicts."""
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    try:
        resp = client.get_post_comments(user=user, post_id=platform_post_id)
    except Exception as exc:
        log.warning(
            "comments.fetch_failed",
            user=user,
            platform_post_id=platform_post_id,
            error=str(exc)[:200],
        )
        return []

    # Upload-Post response shapes vary. Normalise: want a flat list of dicts.
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []
    for key in ("comments", "results", "data", "items"):
        v = resp.get(key)
        if isinstance(v, list):
            return v
    return []


def _post_excerpt(post: PublishedPost) -> str:
    """Short reference to the post we're replying under, for the drafter prompt."""
    url = post.platform_url or post.platform_post_id
    return f"Platform: {post.platform}  ·  URL: {url}"


# ---------------------------------------------------------------------------
# LLM — triage + draft
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """You are triaging incoming comments on a technical founder's posts.
Classify the comment into exactly ONE tier and return it as JSON.

Tiers:
- reply_worthy: genuine engagement worth a reply. Real question, a correction,
  a thoughtful disagreement, someone sharing their relevant experience, an
  industry peer adding perspective, or a warm "this is interesting" from
  someone whose follow-up could turn into a real conversation.
- promo: the commenter is promoting their service/product, DM pitches,
  "great post, check out my newsletter" patterns.
- spam: bot replies, emoji strings, off-topic engagement-farming,
  "follow for follow".
- skip: very low signal — generic "great post!" / "nice!" / single emoji
  that doesn't warrant a reply from the founder.

Return ONLY JSON: {"tier": "<one of the four>", "reason": "<≤12 words>"}
"""

_REPLY_SYSTEM_LINKEDIN = """You are writing a short reply to a comment on a LinkedIn post by a technical founder.

Hard rules — a reply that breaks any of these will be rejected:
- 1-3 sentences max. Never longer. Sharper is better.
- Match the brand voice file verbatim. No marketing verbs, no hype adjectives,
  no engagement-bait questions ("what do you think?").
- No fabricated metrics. Describe decisions, tradeoffs, specifics — not
  outcomes we haven't measured.
- No thanks for comment / "appreciate it" openers.
- Where the commenter asked a specific question, answer it directly.
- Where the commenter made a point, respond to the actual point (agree,
  push back, extend — not a generic "totally agree!").
- If the comment is about a technical detail, add real information.

Output ONLY the reply text. No quotes, no preamble, no JSON."""

# X is a different surface — shorter, more casual, lowercase-friendly. The
# replies that read as human on X are short, specific, and slightly rough.
# Replies that read as AI on X are full sentences with em-dashes, parallel
# structure, and a tidy 3-beat resolution. The rules below ban the AI
# tells aggressively.
_REPLY_SYSTEM_X = """You are writing a reply on X (Twitter) to a comment or mention.

X-specific rules:
- 1-2 sentences. Shorter is better. A single fragment is fine.
- Casual register. Lowercase openings ("yeah,", "agree,", "ha,") are fine
  if they read natural. Never start with "Thanks" or "Appreciate."
- No engagement-bait questions ("what's your take?", "thoughts?", etc.).
- Be concrete. Name the actual tool, the actual number, the actual decision.
  "The agent burned 50k tokens on tool init" beats "the agent struggled."
- It's OK to admit you don't know yet ("haven't fully tested," "still
  figuring out X") — that reads as human, not AI.

ANTI-AI-TELLS — these patterns get rejected as too AI-coded. Hard ban:
- Em-dashes (—) used as mid-sentence pauses. Use a period, comma, or
  start a new sentence instead.
- The "not just X, it's Y" parallel structure. Or "not X. Y." resolution.
- "Here's the thing", "the real lesson", "the real insight",
  "what I've learned is".
- Generic philosophical metaphors: "deciphering signal from chaos",
  "finding signal in the noise", "north star", "shiny object".
- Generic founder-speak: "double down", "lean in", "ship relentlessly",
  "the grind", "build in public" (as a phrase to use; the practice is fine).
- Hype adjectives: "powerful", "robust", "seamless", "game-changing",
  "incredible", "amazing", "huge".
- Three-beat resolution rhythm: setup → complication → tidy lesson.
  Stop after the second beat.

Where the commenter made a specific point, respond to that specific
point. If they asked a question, answer it. If they shared an experience,
share something concrete back — not advice.

Output ONLY the reply text. No quotes, no preamble, no JSON."""


def _reply_system_for(platform: str) -> str:
    """Pick the platform-specific reply prompt."""
    if platform in ("upload_post_x", "x"):
        return _REPLY_SYSTEM_X
    return _REPLY_SYSTEM_LINKEDIN


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _triage_comment(text: str, *, platform: str) -> str:
    """Return one of: reply_worthy | promo | spam | skip."""
    mc = pick("smart" if (settings().openai_api_key or settings().anthropic_api_key) else "cheap")
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": _TRIAGE_SYSTEM},
            {"role": "user", "content": f"Platform: {platform}\nComment: {text}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=512,
        **mc.kwargs,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
        tier = str(data.get("tier", "skip")).strip().lower()
    except (ValueError, TypeError):
        return "skip"
    if tier not in {"reply_worthy", "promo", "spam", "skip"}:
        return "skip"
    return tier


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _draft_reply(
    *,
    brand_id: str,
    platform: str,
    original_post: str,
    comment_text: str,
) -> str:
    """LLM drafts a short reply in brand voice."""
    import pathlib

    cfg = brand_config(brand_id)
    voice_path = cfg.get("voice_prompt_path")
    voice_text = ""
    if voice_path:
        p = pathlib.Path(voice_path)
        if p.exists():
            voice_text = p.read_text()

    # Identity file — concrete specifics the LLM should pull from. Without
    # this, replies fall back to generic AI-consultant agreement.
    identity_path = cfg.get("identity_prompt_path")
    identity_text = ""
    if identity_path:
        p = pathlib.Path(identity_path)
        if p.exists():
            identity_text = p.read_text()

    voice_role = (
        "VOICE IS TEJAS — first-person 'I', personal, lesson/feeling tone."
        if brand_id == "glitch_founder"
        else "VOICE IS GLITCH EXECUTOR — first-person plural 'we', technical, direct."
    )

    identity_block = (
        f"---\n"
        f"WHO YOU ARE — pull specifics from here when drafting. NEVER\n"
        f"invent specifics that aren't in this file.\n\n"
        f"{identity_text}\n"
        if identity_text else ""
    )

    system = (
        f"{voice_text}\n\n"
        f"---\n"
        f"{voice_role}\n"
        f"{identity_block}"
        f"---\n"
        f"{_reply_system_for(platform)}"
    )
    user = (
        f"The original post (ours):\n{original_post}\n\n"
        f"The incoming comment:\n{comment_text}\n\n"
        f"Write the reply."
    )

    mc = pick("smart" if (settings().openai_api_key or settings().anthropic_api_key) else "cheap")
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=1024,
        **mc.kwargs,
    )
    body = (resp.choices[0].message.content or "").strip()
    body = _strip_framing(body)

    # Re-use the same forbidden-terms filter the post generator uses,
    # plus X-specific anti-AI-tells when this is an X reply.
    from glitch_signal.agent.nodes.text_writer import _x_specific_hits

    hits = _forbidden_hits(body)
    if platform in ("upload_post_x", "x"):
        hits = hits + _x_specific_hits(body)

    if hits:
        log.info("comments.reply_forbidden_hits_regen", hits=hits, platform=platform)
        ban = (
            "Your last reply tripped these anti-AI rules: "
            + ", ".join(f'"{h}"' for h in hits)
            + ". Rewrite without any of them. Same idea, different wording. "
            "Read it out loud — if it sounds like a human's casual reply, ship it. "
            "If it sounds like a polished essay, you're not done. "
            "Lowercase casual is fine. A single fragment is fine."
        )
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": body},
                {"role": "user", "content": ban},
            ],
            max_tokens=1024,
            **mc.kwargs,
        )
        body = _strip_framing((resp.choices[0].message.content or "").strip())

    return body


def _strip_framing(text: str) -> str:
    """Remove common 'Here's the reply:' framing the LLM sometimes adds."""
    lines = text.split("\n")
    while lines and lines[0].lower().strip() in (
        "here's the reply:",
        "here is the reply:",
        "reply:",
    ):
        lines = lines[1:]
    out = "\n".join(lines).strip()
    if out.startswith('"') and out.endswith('"'):
        out = out[1:-1].strip()
    return out


# ---------------------------------------------------------------------------
# Discord approval (replaces the Telegram surface as of 2026-04-28)
# ---------------------------------------------------------------------------

async def _send_approval_message(row: CommentReply) -> None:
    """Post a Discord embed for this CommentReply in the configured channel,
    seed ✅/❌ reactions, and persist the message_id so the host-bot plugin
    can dispatch the operator's reaction back to approve_reply / veto_reply.
    """
    channel_id = os.environ.get("SOCIAL_MEDIA_AGENT_CHANNEL_ID", "").strip()
    if not channel_id:
        log.warning("comments.approval.skipped_no_discord_channel", comment_reply_id=row.id)
        return

    from glitch_signal.discord.formatter import comment_reply_embed
    from glitch_signal.discord.rest import add_reaction, post_message

    embed = comment_reply_embed(row, state_override="pending_approval")
    try:
        msg = await post_message(channel_id, embeds=[embed])
    except Exception as exc:
        log.warning(
            "comments.approval.discord_post_failed",
            comment_reply_id=row.id, error=str(exc)[:300],
        )
        return

    msg_id = msg.get("id")
    if msg_id:
        # Seed reactions so operators can click instead of typing them.
        for emoji in ("✅", "❌"):
            try:
                await add_reaction(channel_id, msg_id, emoji)
            except Exception as exc:
                log.debug("comments.approval.seed_reaction_failed", error=str(exc)[:200])

        # Persist the Discord message id so the host-bot plugin can match
        # later reactions back to this row.
        factory = _session_factory()
        async with factory() as session:
            stored = await session.get(CommentReply, row.id)
            if stored:
                stored.discord_message_id = str(msg_id)
                stored.discord_channel_id = str(channel_id)
                stored.updated_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(stored)
                await session.commit()


# ---------------------------------------------------------------------------
# Public API used by the Telegram callback handler
# ---------------------------------------------------------------------------

async def approve_reply(comment_reply_id: str) -> tuple[bool, str]:
    """Called when an operator taps Approve in Telegram. Posts the reply."""
    factory = _session_factory()
    async with factory() as session:
        row = await session.get(CommentReply, comment_reply_id)
        if not row:
            return False, f"comment_reply {comment_reply_id[:8]} not found"
        if row.status not in ("pending_approval", "failed"):
            return False, f"already {row.status}"
        if not row.drafted_reply:
            return False, "no drafted reply"

        cfg = brand_config(row.brand_id)
        user = (cfg.get("platforms", {}).get(row.platform) or {}).get("user")
        if not user:
            return False, f"no Upload-Post user for {row.brand_id}.{row.platform}"

    api_key = settings().upload_post_api_key

    posted_id: str | None = None
    try:
        if row.platform == "upload_post_x":
            # X mentions: Upload-Post hardcodes platform=instagram on the
            # comments/reply endpoint, so it physically can't post a reply
            # on X. Use the native /2/tweets reply path instead.
            from glitch_signal.integrations.x import XClient
            x = XClient(row.brand_id)
            result = await x.post_tweet(
                row.drafted_reply,
                in_reply_to_tweet_id=row.platform_comment_id,
            )
            posted_id = result.tweet_id
        else:
            # IG (and any future Upload-Post-supported platform) goes
            # through the vendor reply_to_comment endpoint.
            if not api_key:
                return False, "UPLOAD_POST_API_KEY unset"
            resp = await asyncio.to_thread(
                _post_reply,
                api_key,
                user,
                row.platform_comment_id,
                row.drafted_reply,
            )
            posted_id = (
                resp.get("id") or resp.get("reply_id") or resp.get("request_id")
                if isinstance(resp, dict) else None
            )
    except Exception as exc:
        async with _session_factory()() as session:
            row = await session.get(CommentReply, comment_reply_id)
            if row:
                row.status = "failed"
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(row)
                await session.commit()
        return False, f"reply_to_comment failed: {exc}"

    async with _session_factory()() as session:
        row = await session.get(CommentReply, comment_reply_id)
        if row:
            row.status = "posted"
            row.posted_reply_id = posted_id
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(row)
            await session.commit()

    log.info("comments.reply.posted", comment_reply_id=comment_reply_id, posted_id=posted_id)
    return True, "Reply posted."


async def veto_reply(comment_reply_id: str) -> tuple[bool, str]:
    """Operator tapped Skip. Mark the row ignored and move on."""
    factory = _session_factory()
    async with factory() as session:
        row = await session.get(CommentReply, comment_reply_id)
        if not row:
            return False, f"comment_reply {comment_reply_id[:8]} not found"
        row.status = "ignored"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        session.add(row)
        await session.commit()
    return True, "Skipped."


def _post_reply(api_key: str, user: str, comment_id: str, message: str) -> dict:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    return client.reply_to_comment(user=user, comment_id=comment_id, message=message)
