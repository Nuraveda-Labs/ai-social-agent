"""Storyboard node — breaks a script into 5-8 individual shots.

Each shot has: visual description, duration (3-8s), model_hint.
The storyboard is persisted as JSON in ContentScript.shots.
"""
from __future__ import annotations

import json

import litellm
import structlog

from glitch_signal.agent.llm import pick
from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import settings
from glitch_signal.db.models import ContentScript
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

VALID_MODEL_HINTS = {"cinematic", "realistic", "text_in_video", "fast"}

_SYSTEM_PROMPT = """You are a storyboard artist for short-form technical video content.

Given a script for a 15-90s YouTube Short / Instagram Reel about an algorithmic trading AI platform,
break it into 5-8 individual shots for AI video generation.

Visual style: dark (#0a0a0f background), neon green highlights (#00ff88), electric blue (#0088ff).
Cobra mascot appears in at least one shot. Cinematic but technical.

For each shot, choose a model_hint:
- cinematic: hero shots, cobra mascot, brand reveals, atmospheric
- realistic: product demos, person-at-computer, realistic motion
- text_in_video: code on screen, architecture diagrams, text overlays
- fast: quick cuts, data visualizations, montages

Output valid JSON only:
{
  "shots": [
    {
      "visual": "detailed visual description for the AI video model prompt",
      "duration_s": 5,
      "model_hint": "cinematic | realistic | text_in_video | fast"
    }
  ]
}

Rules:
- Total duration of all shots should be 60-90 seconds
- Each shot: 3-8 seconds
- First shot must be a hook — visually striking, no context needed
- Last shot: clean brand close (cobra watermark prominent, CTA text)
- "visual" should be detailed enough to prompt a video model directly
"""


async def storyboard_node(state: SignalAgentState) -> SignalAgentState:
    script_id = state.get("script_id")
    script_body = state.get("script_body", "")
    content_type = state.get("content_type", "technical")

    if not script_id or not script_body:
        return {**state, "error": "storyboard: missing script_id or script_body"}

    shots = await _generate_shots(script_body, content_type)

    # Persist shots back to ContentScript
    factory = _session_factory()
    async with factory() as session:
        cs = await session.get(ContentScript, script_id)
        if cs:
            cs.shots = json.dumps(shots)
            session.add(cs)
            await session.commit()

    log.info("storyboard.done", script_id=script_id, n_shots=len(shots))
    return {**state, "shots": shots}


async def _generate_shots(script_body: str, content_type: str) -> list[dict]:
    if settings().is_dry_run:
        return [
            {"visual": f"[dry-run shot {i+1}]", "duration_s": 5, "model_hint": "fast"}
            for i in range(6)
        ]

    mc = pick("cheap")
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Content type: {content_type}\n\nScript:\n{script_body}",
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=600,
        **mc.kwargs,
    )

    raw = resp.choices[0].message.content or '{"shots": []}'
    data = json.loads(raw)
    shots = data.get("shots", [])

    # Validate and clamp
    valid: list[dict] = []
    for shot in shots:
        hint = str(shot.get("model_hint", "fast")).lower()
        if hint not in VALID_MODEL_HINTS:
            hint = "fast"
        valid.append({
            "visual": str(shot.get("visual", ""))[:500],
            "duration_s": max(3, min(8, int(shot.get("duration_s", 5)))),
            "model_hint": hint,
        })

    return valid[:8]  # hard cap at 8 shots
