"""ScriptWriter node — generates a 60-90s short-form video script from a Signal."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import litellm
import structlog

from social_signal.agent.llm import pick
from social_signal.agent.state import SignalAgentState
from social_signal.config import settings
from social_signal.db.models import ContentScript, Signal
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

VALID_CONTENT_TYPES = {"cinematic", "product", "technical", "data"}

_SYSTEM_PROMPT = """You are the social media voice for this project — an algorithmic trading AI platform
built by a deeply technical solo founder (Tejas Karan Agrawal) in <region>.

Brand voice rules (non-negotiable):
- Technical and direct. No marketing hype. No "thrilled to announce". No emoji walls.
- First-person plural for company posts. First-person singular for founder posts.
- Show, don't tell: screenshots, code snippets, and measurable results > opinion takes.
- Never post if there's nothing real to say. Silence > slop.

Visual brand: dark base (#0a0a0f), neon green accent (#00ff88), electric blue (#0088ff).
Cobra mascot is canonical — reference it where natural.

For short-form video (15–90s Shorts/Reels/TikTok format):
- Hook in first 3 seconds. No intros.
- Max 90 seconds total script.
- End with a concrete call-to-action (repo link, site, or next post).

Few-shot examples of the founder's voice (from his actual commits):
- "cod-confirm: add per-shop DND window override — some clients run 24h, TRAI rules don't apply"
- "rename ouroboros strategy positions to snakes — it's on brand and easier to debug"
- "ensemble: bump Mamba weight to 0.18 after last week's XAU run — 3.2% edge vs equal-weight"

You must output valid JSON only. No markdown fences, no prose outside JSON.

Output schema:
{
  "script_body": "full narration script for the video (60-90 seconds when spoken at 130wpm)",
  "content_type": "cinematic | product | technical | data",
  "key_visuals": ["visual description 1", "visual description 2", ...]
}

content_type guide:
- cinematic: brand story, mascot, identity, culture
- product: live product demo, feature walkthrough, UI
- technical: architecture explanation, code walkthrough, system design
- data: trading performance, metrics, charts, results
"""


async def script_writer_node(state: SignalAgentState) -> SignalAgentState:
    signal_id = state.get("signal_id")
    platform = state.get("platform", "youtube_shorts")

    factory = _session_factory()
    async with factory() as session:
        signal = await session.get(Signal, signal_id)
        if not signal:
            return {**state, "error": f"script_writer: Signal {signal_id} not found"}

        brand_id = (
            state.get("brand_id")
            or getattr(signal, "brand_id", None)
            or settings().default_brand_id
        )

        script_id, script_body, content_type, key_visuals = await _generate_script(
            signal, platform
        )

        cs = ContentScript(
            id=script_id,
            brand_id=brand_id,
            signal_id=signal_id,
            platform=platform,
            script_body=script_body,
            content_type=content_type,
            key_visuals=json.dumps(key_visuals),
            shots="[]",
            status="draft",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(cs)

        signal.status = "scripting"
        session.add(signal)
        await session.commit()

    log.info(
        "script_writer.done",
        script_id=script_id,
        brand_id=brand_id,
        content_type=content_type,
        n_visuals=len(key_visuals),
    )
    return {
        **state,
        "brand_id": brand_id,
        "script_id": script_id,
        "script_body": script_body,
        "content_type": content_type,
        "key_visuals": key_visuals,
    }


async def _generate_script(
    signal: Signal, platform: str
) -> tuple[str, str, str, list[str]]:
    if settings().is_dry_run:
        return (
            str(uuid.uuid4()),
            f"[dry-run script] {signal.summary}",
            "technical",
            ["[dry-run visual 1]", "[dry-run visual 2]"],
        )

    user_msg = (
        f"Platform: {platform}\n"
        f"Signal summary: {signal.summary}\n"
        f"Source: {signal.source} — {signal.source_ref}\n\n"
        "Write the video script for this signal."
    )

    mc = pick("smart")
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        max_tokens=800,
        **mc.kwargs,
    )

    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)

    script_body = str(data.get("script_body", "")).strip()
    content_type = str(data.get("content_type", "technical")).strip().lower()
    key_visuals = list(data.get("key_visuals", []))

    if content_type not in VALID_CONTENT_TYPES:
        content_type = "technical"

    return str(uuid.uuid4()), script_body, content_type, key_visuals
