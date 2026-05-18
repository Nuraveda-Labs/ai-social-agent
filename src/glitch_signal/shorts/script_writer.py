"""LLM script writer for YouTube Shorts.

Output schema (the only source of truth — visuals + voice both consume it):

    {
      "hook":     str,         # 1 sentence, ≤ 12 words. Must stop the scroll.
      "segments": [
        {
          "spoken":  str,      # 1-2 sentences spoken by the voiceover (~6-12s)
          "visual":  str,      # gpt-image-2 prompt for the still backing this beat
        },
        ...   (3-5 segments)
      ],
      "cta": str,              # 1 sentence closer
      "estimated_seconds": int # total duration estimate (15-60 typical)
    }

The script is grounded in the brand's voice + identity files (same files
the comment / strategic-reply drafters use), so every short reads as
either Tejas (founder voice) or the lab (brand voice) — never as
generic AI narration.
"""
from __future__ import annotations

import json
import pathlib

import litellm
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.agent.llm import pick
from glitch_signal.config import brand_config, settings

log = structlog.get_logger(__name__)


_SCRIPT_SYSTEM = """You write a short-form vertical video script (YouTube
Shorts / TikTok / IG Reels). Total length ≤ 60 seconds.

Structure:
  1. Hook (1 sentence, ≤ 12 words). Must reference a SPECIFIC concrete
     thing — not "AI is changing marketing", but "I cut voice-agent
     dead air from 10 seconds to 130 milliseconds."
  2. 3-5 body segments. Each segment = 1-2 short sentences spoken aloud
     (~6-12 seconds each at conversational pace), plus one visual prompt
     for the still backing it.
  3. CTA closer (1 sentence). Direct the viewer to the github / website.
     Never end with "what do you think?" or any engagement-bait question.

Hard rules — a script that breaks any of these will be rejected:
  - SPECIFIC over vague. Pull real specifics from the identity file
    when one is provided. NEVER invent metrics or specifics.
  - No marketing verbs / hype adjectives. (See voice file.)
  - No exclamation closers. End on a beat.
  - Keep spoken text COLLOQUIAL — read it out loud; if it sounds like
    a press release, rewrite.
  - Visual prompts: dark editorial tech aesthetic, NO humans / faces /
    clipart / stock photos. Geometric, abstract, brand-coded.

Visual prompt format (per segment, fed directly to gpt-image-2):
  Describe ONE concrete graphical motif that illustrates the spoken
  beat — a chart, a code-frame, a glyph, a diagram. 2-3 sentences max.
  Always include: "1080x1920 vertical, dark editorial tech, no humans,
  no clipart" so the aesthetic stays consistent across segments.

Output VALID JSON only matching the schema. No prose, no markdown."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def write_script(
    *,
    brand_id: str,
    topic: str,
    target_seconds: int = 45,
) -> dict:
    """Produce a short-video script grounded in brand voice + identity."""
    cfg = brand_config(brand_id)

    voice_text = _load_file(cfg.get("voice_prompt_path"))
    identity_text = _load_file(cfg.get("identity_prompt_path"))

    voice_role = (
        "VOICE IS TEJAS — first-person 'I', personal, lesson/feeling tone."
        if brand_id == "glitch_founder"
        else "VOICE IS GLITCH EXECUTOR — first-person plural 'we', technical, direct."
    )

    identity_block = (
        f"---\nWHO YOU ARE — pull specifics from here. NEVER invent\n"
        f"specifics that aren't in this file.\n\n{identity_text}\n"
        if identity_text else ""
    )

    system = (
        f"{voice_text}\n\n"
        f"---\n{voice_role}\n"
        f"{identity_block}"
        f"---\n{_SCRIPT_SYSTEM}"
    )
    user = (
        f"Topic / angle: {topic}\n"
        f"Target duration: ~{target_seconds} seconds total.\n\n"
        f"Write the script JSON."
    )

    s = settings()
    tier = "smart" if (s.openai_api_key or s.anthropic_api_key) else "cheap"
    mc = pick(tier)
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
        **mc.kwargs,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"script JSON parse failed: {exc} :: {raw[:200]!r}") from exc

    data = _normalize(data)
    _validate(data)
    log.info(
        "shorts.script.done",
        brand_id=brand_id, topic=topic[:80],
        segments=len(data.get("segments", [])),
        est_seconds=data.get("estimated_seconds"),
    )
    return data


def _normalize(data: dict) -> dict:
    """Coerce common LLM shape drifts into the canonical schema.

    Variants we've seen:
      - top-level "script" or "video" wrapper
      - segments as "body" / "beats" / "scenes"
      - per-segment "text"/"narration"/"voice" instead of "spoken"
      - per-segment "image"/"image_prompt"/"prompt" instead of "visual"
      - cta as "outro" / "call_to_action" / "ending"
    """
    if not isinstance(data, dict):
        return {}
    # Unwrap if the model nested under "script" / "video"
    for wrapper in ("script", "video", "Short"):
        if wrapper in data and isinstance(data[wrapper], dict):
            data = data[wrapper]
            break

    # Normalize segments key
    seg_key_candidates = ("segments", "body", "beats", "scenes", "shots")
    seg_list = None
    for k in seg_key_candidates:
        if isinstance(data.get(k), list):
            seg_list = data[k]
            break
    if seg_list is None:
        seg_list = []

    # Normalize each segment's keys
    norm_segments = []
    for s in seg_list:
        if not isinstance(s, dict):
            continue
        spoken = (
            s.get("spoken") or s.get("text") or s.get("narration")
            or s.get("voice") or s.get("voiceover") or ""
        )
        visual = (
            s.get("visual") or s.get("image") or s.get("image_prompt")
            or s.get("prompt") or s.get("visual_prompt") or ""
        )
        if spoken and visual:
            norm_segments.append({"spoken": spoken, "visual": visual})

    cta = (
        data.get("cta") or data.get("outro") or data.get("call_to_action")
        or data.get("ending") or "github.com/glitch-exec-labs"
    )

    return {
        "hook": data.get("hook") or data.get("opener") or "",
        "segments": norm_segments,
        "cta": cta,
        "estimated_seconds": data.get("estimated_seconds")
            or data.get("duration_seconds")
            or data.get("duration") or 45,
    }


def _validate(data: dict) -> None:
    """Cheap structural check so downstream stages don't hit None."""
    if not isinstance(data, dict):
        raise ValueError("script: not a dict")
    if not isinstance(data.get("hook"), str) or not data["hook"].strip():
        raise ValueError("script: missing/empty hook")
    segs = data.get("segments")
    if not isinstance(segs, list) or not (2 <= len(segs) <= 6):
        raise ValueError(
            f"script: segments must be a 2-6 item list, got "
            f"{type(segs).__name__} with {len(segs) if isinstance(segs, list) else '?'} items"
        )
    for i, s in enumerate(segs):
        if not isinstance(s, dict):
            raise ValueError(f"script.segments[{i}] not a dict")
        if not isinstance(s.get("spoken"), str) or not s["spoken"].strip():
            raise ValueError(f"script.segments[{i}].spoken missing")
        if not isinstance(s.get("visual"), str) or not s["visual"].strip():
            raise ValueError(f"script.segments[{i}].visual missing")
    if not isinstance(data.get("cta"), str):
        data["cta"] = "github.com/glitch-exec-labs"


def _load_file(path: str | None) -> str:
    if not path:
        return ""
    p = pathlib.Path(path)
    return p.read_text() if p.exists() else ""
