"""TextWriter node — generates a text post per enabled text platform for a Signal.

Replaces the script_writer → storyboard → video_generator chain for brands
with content_format="text" (glitch_executor, glitch_founder). Output per
signal is one ContentScript per enabled upload_post_* text platform, each
paired with a ScheduledPost (status=pending_veto) so the existing Telegram
approve/veto + scheduler dispatch works unchanged.

The LLM prompt is brand-aware:
  - brand voice prompt          (brand/prompts/<brand>_voice.md)
  - platform playbook           (brand/prompts/platform_playbook.md)
  - per-platform rules enforced (char limits, link placement)
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import litellm
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.agent.llm import pick
from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import ContentScript, ScheduledPost, Signal
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

VETO_WINDOW_HOURS = 48

# Which platform keys in a brand config emit text posts (vs video).
TEXT_PLATFORM_KEYS = ("upload_post_x", "upload_post_linkedin")

# Per-platform hard limits. Kept conservative; LLM targets ~80% of max.
PLATFORM_CHAR_LIMIT = {
    "upload_post_x": 280,
    "upload_post_linkedin": 2800,   # LinkedIn allows 3000; sweet spot ~1300-2500
}
# Target length (mid-sweet-spot). LLM instructed to stay close.
PLATFORM_TARGET_CHARS = {
    "upload_post_x": 250,
    "upload_post_linkedin": 1800,
}

# ---------------------------------------------------------------------------
# Forbidden-terms filter: post-hoc check that catches hype adjectives and
# marketing verbs the LLM slips in even when the system prompt forbids them.
# Match is case-insensitive, whole-word. A single hit triggers one
# regeneration round with the offending phrases added as an explicit ban.
# ---------------------------------------------------------------------------

_FORBIDDEN_WORDS: tuple[str, ...] = (
    # Hype adjectives
    "seamlessly", "seamless",
    "robust",
    "fluid",
    "powerful",
    "refined",
    "sleek",
    "cutting-edge",
    "game-changing", "game-changer",
    "revolutionary",
    "industry-leading",
    "next-generation", "next-gen",
    "state-of-the-art",
    "world-class",
    "best-in-class",
    "turnkey",
    # Marketing verbs that imply measured outcomes
    "delivers",
    "boosts",
    "supercharges",
    "unlocks",
    "empowers",
    # Founder-speak
    "thrilled",
    "humbled",
    "grateful for the journey",
    "proud to announce", "excited to announce", "excited to share",
    "stay tuned",
    # Corporate gloss
    "quest for",
    "in our quest",
    "our journey",
    "on our journey",
    # AI-tell phrases (added 2026-04-29). Things LLMs reach for that
    # human writers basically never type. Each one tanks the human-feel
    # of a post on its own.
    "the real lesson",
    "the real insight",
    "here's the thing",
    "here's the kicker",
    "what i've learned is",
    "what i've come to realize",
    "deciphering signal",
    "signal from chaos",
    "signal in the noise",
    "north star",
    "shiny model", "shiny object",
    "double down", "doubling down",
    "lean in",
    "ship relentlessly",
    "the grind",
    "saving grace",
    "let it iterate",
    "stick with what works",
    "rinse and repeat",
    # AI-consultant tells (added 2026-04-29 after a strategic-reply
    # draft slid through with phrases no human posts on social).
    "fascinating",
    "resonates",
    "resonate",
    "synergy",
    "synergize",
    "blending",
    "blends",
    "blend",
    "structural strengths",
    "personal storytelling",
    "human touch",
    "authentic voice",
    "more authentic",
    "creates content that",
    "in my experience" + ",",  # comma form is the AI tell; the bare phrase is fine
    "it is fascinating",
    "it's fascinating",
    "absolutely",
    "delve",
    "delves",
    "delving",
    "navigate the",
    "navigating the",
    "the journey of",
    "embark",
    "embarking",
    "elevate",
    "elevates",
    "transformative",
    "leverages",  # specific to AI essays — humans say "uses"
    "harnessing",
    "ushers in",
    # Discovered in failed strategic-reply drafts (Apr 29).
    "amazing",
    "amazingly",
    "eye-opening",
    "noticeably",
    "noteworthy",
    "key takeaway",
    "key learning",
    "boundless",
    "unlocking",
    "uncover",
    "uncovers",
    "uncovering",
    "exemplifies",
    "epitomizes",
    "remarkable",
)

# Engagement-bait question patterns. These sneak in on short-form X posts.
_ENGAGEMENT_BAIT_PATTERNS: tuple[str, ...] = (
    "what do you think",
    "what's your take",
    "whats your take",
    "thoughts?",
    "agree?",
    "am i missing something?",
    "what would you do",
    "curious what you think",
)


def _forbidden_hits(body: str) -> list[str]:
    """Return every forbidden term / bait pattern that appears in the body."""
    import re
    hits: list[str] = []
    lowered = body.lower()
    for term in _FORBIDDEN_WORDS:
        # whole-word match for single words, substring for multi-word phrases
        if " " in term or "-" in term:
            if term.lower() in lowered:
                hits.append(term)
        else:
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                hits.append(term)
    for pattern in _ENGAGEMENT_BAIT_PATTERNS:
        if pattern in lowered:
            hits.append(pattern)
    return hits


def _x_specific_hits(body: str) -> list[str]:
    """Extra checks that apply only on short-form X content.

    Em-dashes and "not X, but Y" parallels are legitimate in long-form
    LinkedIn carousels but read as AI tells in a 1-2 sentence X reply.
    Run alongside _forbidden_hits for the X reply path.
    """
    import re
    hits: list[str] = []
    # Em-dash (U+2014) used as a mid-sentence pause. The actual character,
    # not " - " or " -- ". This is the single highest-signal AI tell on X.
    if "—" in body:
        hits.append("em-dash (—) reads as AI on X")
    # "It's not X, it's Y" parallel structure.
    if re.search(r"\bit'?s not (just )?\w+[\s,]+it'?s \w+", body, re.IGNORECASE):
        hits.append("'it's not X, it's Y' parallel")
    # "X is real. Y is real." — the AI consultant cadence.
    if re.search(r"\b(\w+) is real\.\s+(\w+) is real\b", body, re.IGNORECASE):
        hits.append("'X is real. Y is real.' cadence")
    return hits


async def text_writer_node(state: SignalAgentState) -> SignalAgentState:
    """Produce one text post per enabled text platform for the given Signal.

    Requires `signal_id` in state — scout_node populates it to the top
    unprocessed Signal for the brand on each run.
    """
    signal_id = state.get("signal_id") or ""
    if not signal_id:
        log.warning("text_writer.no_signal_id")
        return {**state, "error": "text_writer: no signal_id in state"}

    factory = _session_factory()
    async with factory() as session:
        signal = await session.get(Signal, signal_id)
        if not signal:
            return {**state, "error": f"text_writer: Signal {signal_id} not found"}

        brand_id = (
            state.get("brand_id")
            or getattr(signal, "brand_id", None)
            or settings().default_brand_id
        )
        cfg = brand_config(brand_id)
        platforms_cfg = cfg.get("platforms", {}) or {}

        # Which text platforms are enabled for this brand right now?
        enabled_platforms = [
            key for key in TEXT_PLATFORM_KEYS
            if (platforms_cfg.get(key) or {}).get("enabled")
        ]
        if not enabled_platforms:
            log.info("text_writer.no_text_platforms_enabled", brand_id=brand_id)
            # Mark the signal scripted so we don't loop on it
            signal.status = "scripted"
            session.add(signal)
            await session.commit()
            return {**state, "brand_id": brand_id, "error": "no text platforms enabled"}

        voice_text = _load_voice(cfg)
        playbook_text = _load_playbook(cfg)

        now = datetime.now(UTC).replace(tzinfo=None)
        veto_deadline = now + timedelta(hours=VETO_WINDOW_HOURS)

        created_ids: list[str] = []
        for platform_key in enabled_platforms:
            short_platform = platform_key.replace("upload_post_", "")
            try:
                body = await _write_post(
                    signal=signal,
                    brand_id=brand_id,
                    platform_short=short_platform,
                    voice_text=voice_text,
                    playbook_text=playbook_text,
                )
            except Exception as exc:
                log.warning(
                    "text_writer.generation_failed",
                    signal_id=signal_id,
                    platform=platform_key,
                    error=str(exc)[:200],
                )
                continue

            body = _enforce_char_limit(body, platform_key)

            cs = ContentScript(
                id=str(uuid.uuid4()),
                brand_id=brand_id,
                signal_id=signal.id,
                platform=platform_key,
                script_body=body,
                content_type="text",
                key_visuals="[]",
                shots="[]",
                status="draft",
            )
            session.add(cs)

            sp = ScheduledPost(
                id=str(uuid.uuid4()),
                brand_id=brand_id,
                asset_id=None,           # text posts have no video asset
                script_id=cs.id,         # direct link to ContentScript
                platform=platform_key,
                scheduled_for=veto_deadline,  # auto-publish when veto window lapses
                status="pending_veto",
                veto_deadline=veto_deadline,
            )
            session.add(sp)
            created_ids.append((cs.id, sp.id, platform_key, body))

        signal.status = "scripted"
        session.add(signal)
        await session.commit()

    log.info(
        "text_writer.done",
        signal_id=signal_id,
        brand_id=brand_id,
        posts_created=len(created_ids),
    )

    # Fire Telegram preview messages for each generated post
    for _cs_id, sp_id, platform_key, body in created_ids:
        await _send_text_preview(
            sp_id=sp_id,
            brand_id=brand_id,
            platform_key=platform_key,
            body=body,
            veto_deadline=veto_deadline,
        )

    return {
        **state,
        "brand_id": brand_id,
        "preview_sent": True,
        "veto_deadline": veto_deadline.isoformat(),
    }


# ---------------------------------------------------------------------------
# LLM call (with retry for Gemini 503 bursts)
# ---------------------------------------------------------------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _call_llm(mc, messages: list[dict]) -> str:
    resp = await litellm.acompletion(
        model=mc.model,
        messages=messages,
        max_tokens=4096,        # headroom for Gemini thinking tokens
        **mc.kwargs,
    )
    return (resp.choices[0].message.content or "").strip()


async def _write_post(
    *,
    signal: Signal,
    brand_id: str,
    platform_short: str,
    voice_text: str,
    playbook_text: str,
) -> str:
    """Generate the post body for a single (signal, platform) pair."""
    target_chars = PLATFORM_TARGET_CHARS.get(f"upload_post_{platform_short}", 1500)
    limit = PLATFORM_CHAR_LIMIT.get(f"upload_post_{platform_short}", 2800)

    system_prompt = _build_system_prompt(
        brand_id=brand_id,
        platform_short=platform_short,
        voice_text=voice_text,
        playbook_text=playbook_text,
        target_chars=target_chars,
        limit=limit,
    )
    user_prompt = _build_user_prompt(signal)

    # Prefer smart tier (OpenAI gpt-4o → Claude) when any high-quality
    # provider key is configured; fall back to cheap (Gemini Flash) only if
    # nothing smart is available. Voice guard rails are followed much more
    # reliably by smart-tier models.
    s_ = settings()
    tier = "smart" if (s_.openai_api_key or s_.anthropic_api_key) else "cheap"
    mc = pick(tier)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw = await _call_llm(mc, messages)
    body = _extract_body(raw)

    # Post-hoc forbidden-terms check. If the LLM slipped hype adjectives,
    # marketing verbs, or engagement-bait questions past the system prompt,
    # regenerate ONCE with an explicit inline ban. If it slips again, log a
    # warning and ship the second draft — we don't want an infinite retry
    # loop burning tokens on a stubborn signal.
    hits = _forbidden_hits(body)
    if hits:
        log.info(
            "text_writer.forbidden_hits_regen",
            platform=f"upload_post_{platform_short}",
            brand_id=brand_id,
            hits=hits,
        )
        ban_msg = (
            "Your last draft used banned phrases: "
            + ", ".join(f'"{h}"' for h in hits)
            + ". Rewrite the post without any of these phrases and without "
            "any other hype adjectives or marketing verbs. Keep the same "
            "factual content — just change the wording."
        )
        retry_messages = messages + [
            {"role": "assistant", "content": body},
            {"role": "user", "content": ban_msg},
        ]
        raw = await _call_llm(mc, retry_messages)
        body = _extract_body(raw)
        second_hits = _forbidden_hits(body)
        if second_hits:
            log.warning(
                "text_writer.forbidden_hits_persisted",
                platform=f"upload_post_{platform_short}",
                brand_id=brand_id,
                hits=second_hits,
            )
    return body


def _build_system_prompt(
    *,
    brand_id: str,
    platform_short: str,
    voice_text: str,
    playbook_text: str,
    target_chars: int,
    limit: int,
) -> str:
    platform_rules = {
        "x": (
            f"Platform: X (Twitter). HARD LIMIT {limit} characters — anything longer will be rejected.\n"
            f"Aim for {target_chars} characters. Short, punchy, one clear hook.\n"
            "Rules from playbook:\n"
            "- First 70-100 chars are the hook — they decide whether people read on.\n"
            "- Max 2 hashtags. Place them at the end, natural only.\n"
            "- NO external link in post body for Premium reach (link only if essential).\n"
            "- Specific numbers beat vague claims (say '14d ROAS 1.24×' not 'great ROAS').\n"
            "- ABSOLUTELY NO engagement-bait closing questions: 'What do you think?',\n"
            "  'Thoughts?', 'Agree?', 'What's your take?'. These patterns are forbidden.\n"
            "  End on a declarative statement or a link instead.\n"
        ),
        "linkedin": (
            f"Platform: LinkedIn. Max {limit} chars; aim for {target_chars} (sweet spot 1300-2500).\n"
            "Rules from playbook:\n"
            "- First 140 chars must be the hook (mobile cutoff above 'see more').\n"
            "- One sentence per line. White space between ideas.\n"
            "- 3-5 hashtags at the end (never scattered).\n"
            "- No external link at the top — put it plain-text near the end if needed.\n"
            "- Concrete decisions and specifics > abstract claims.\n"
            "- ABSOLUTELY NO engagement-bait closing questions: 'What do you think?',\n"
            "  'Thoughts?', 'Agree?', 'Curious what you think?'. Ending on a genuine\n"
            "  open question about a specific technical decision is fine. Generic\n"
            "  engagement-farming questions are not.\n"
        ),
    }.get(platform_short, "")

    # Per-brand voice enforcement. The voice file says this, but LLMs diffuse
    # single-sentence rules. Repeat the most important ones at the top of the
    # prompt in bold "do not" form.
    if brand_id == "glitch_founder":
        voice_rules = (
            "VOICE IS TEJAS (first-person singular only):\n"
            "- Use 'I', 'me', 'my' exclusively. NEVER 'we', 'our', 'our team'.\n"
            "- If the signal describes team work, reframe as what I personally did,\n"
            "  learned, noticed, or decided. Example — not 'we launched Priya',\n"
            "  say 'I shipped Priya this week' or 'I've been working on Priya'.\n"
            "- This is a personal learning log, not a company announcement.\n"
            "- Lead with a feeling, an observation, or a specific moment — not a fact.\n"
        )
    else:
        voice_rules = (
            "VOICE IS GLITCH EXECUTOR (the lab, first-person plural):\n"
            "- Use 'we', 'our', 'we shipped'. Do not write in the first-person 'I'.\n"
            "- Technical and factual. Describe the build, not the results we have\n"
            "  not yet measured.\n"
        )

    return (
        f"{voice_text}\n\n"
        f"---\n"
        f"{voice_rules}\n"
        f"---\n"
        f"{platform_rules}\n"
        f"---\n"
        f"Platform playbook (condensed):\n{playbook_text[:3000]}\n"
        f"---\n"
        "HARD GUARD RAILS — any post violating these will be rejected at review:\n"
        "1. Do NOT claim any measured outcome (percent, ROI, reduction, growth, savings, "
        "conversion rate, revenue, etc.) unless the signal you were given explicitly "
        "contains that number. If the signal doesn't state 'X reduced Y by Z%', you don't "
        "get to state it either. Describe the BUILD, not the RESULT.\n"
        "2. Do NOT use marketing verbs like 'reduces', 'boosts', 'delivers', 'improves'. "
        "Use 'targets', 'aims to', 'was built to', 'is running', 'is in production'.\n"
        "3. Do NOT use hype adjectives: game-changing, revolutionary, cutting-edge, "
        "industry-leading, robust, powerful, seamless.\n"
        "4. No 'excited to announce' / 'thrilled to share' / 'proud to introduce'.\n"
        "---\n"
        "Output ONLY the post body. No prefix like 'Here's the post:'. No JSON. No markdown fences.\n"
        "Just the exact text that will be published."
    )


def _build_user_prompt(signal: Signal) -> str:
    return (
        f"Write a post about this shipped work:\n\n"
        f"Source: {signal.source} (ref: {signal.source_ref})\n"
        f"Summary: {signal.summary}\n\n"
        f"The signal was scored novelty={signal.novelty_score:.2f} — tune the tone "
        "to match the actual significance. Don't over-hype."
    )


def _extract_body(raw: str) -> str:
    """Strip common LLM framing like 'Here's the post:' or markdown fences."""
    text = raw.strip()
    # Strip code fences if the model wrapped it
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 3:
            # take the body of the first fenced block
            text = parts[1]
            # drop leading language tag line if present ("text\n", "markdown\n")
            if "\n" in text:
                first_line, rest = text.split("\n", 1)
                if len(first_line) <= 20 and " " not in first_line:
                    text = rest
    # Strip framing lines at the top
    lines = text.split("\n")
    while lines and lines[0].lower().strip() in (
        "here is the post:",
        "here's the post:",
        "post:",
    ):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _enforce_char_limit(body: str, platform_key: str) -> str:
    limit = PLATFORM_CHAR_LIMIT.get(platform_key)
    if limit and len(body) > limit:
        log.warning(
            "text_writer.truncating_body",
            platform=platform_key,
            original_len=len(body),
            limit=limit,
        )
        return body[: limit - 1].rstrip() + "…"
    return body


# ---------------------------------------------------------------------------
# Brand voice + playbook loaders
# ---------------------------------------------------------------------------

def _load_voice(cfg: dict) -> str:
    path = cfg.get("voice_prompt_path")
    if not path:
        return ""
    p = pathlib.Path(path)
    return p.read_text() if p.exists() else ""


def _load_playbook(cfg: dict) -> str:
    path = cfg.get("platform_playbook_path")
    if not path:
        return ""
    p = pathlib.Path(path)
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------------------
# Discord preview (text version — no video attachment)
# ---------------------------------------------------------------------------

async def _send_text_preview(
    *,
    sp_id: str,
    brand_id: str,
    platform_key: str,
    body: str,
    veto_deadline: datetime,
) -> None:
    """Mark the row pending; the host-bot Discord plugin polls for these
    and posts the approval embed in #grow-social. Telegram surface
    retired 2026-05-01.
    """
    if settings().is_dry_run:
        log.info("text_writer.preview.dry_run", sp_id=sp_id, platform=platform_key)
        return

    # The Discord host-bot plugin (glitch-discord-bot/social_media_agent_integration.py)
    # already polls for pending rows every 30s and posts the embed itself.
    # Nothing to do here at preview time — this function is retained so
    # legacy callers don't break.
    log.info(
        "text_writer.preview.queued_for_discord",
        sp_id=sp_id,
        brand_id=brand_id,
        platform=platform_key,
        chars=len(body),
        veto_deadline=str(veto_deadline),
    )
