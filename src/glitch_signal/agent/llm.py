"""LiteLLM-routed model selection.

Tiers and preferred providers (first with a key wins):

  cheap   → OpenAI gpt-4o-mini → Gemini 2.5 Flash
            (novelty scoring, storyboard breakdown — high volume, low depth)

  smart   → OpenAI gpt-4o      → Claude Sonnet 4.6 → Gemini 2.5 Flash
            (script writing, text-post copywriting, carousel slide content,
             ORM classification — instruction-following matters)

  heavy   → Gemini 2.5 Pro     → Vertex 2.5 Pro fallback
            (video QC vision, long-form reasoning)

Why OpenAI first on cheap/smart: gpt-4o family follows explicit "do not"
lists and character-limit constraints more reliably than Gemini Flash in
our testing, which matters a lot for the voice-guard-rails pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass

from glitch_signal.config import settings


@dataclass(frozen=True)
class ModelChoice:
    model: str
    kwargs: dict


def pick(tier: str = "cheap") -> ModelChoice:
    s = settings()

    if tier == "smart":
        if s.openai_api_key:
            return ModelChoice(
                model=s.openai_smart_model,   # e.g. "gpt-4o"
                kwargs={"api_key": s.openai_api_key},
            )
        if s.anthropic_api_key:
            return ModelChoice(
                model="claude-sonnet-4-6",
                kwargs={"api_key": s.anthropic_api_key},
            )
        # Fall through to Gemini Flash below — not ideal for smart tier,
        # but keeps the graph working when nothing else is configured.

    if tier == "heavy":
        if s.vertex_project:
            return ModelChoice(
                model="vertex_ai/gemini-2.5-pro",
                kwargs={
                    "vertex_project": s.vertex_project,
                    "vertex_location": s.vertex_location,
                },
            )
        return ModelChoice(
            model="gemini/gemini-2.5-pro",
            kwargs={"api_key": s.google_api_key},
        )

    # cheap (default) — and smart fallback
    if s.openai_api_key:
        return ModelChoice(
            model=s.openai_cheap_model,      # e.g. "gpt-4o-mini"
            kwargs={"api_key": s.openai_api_key},
        )
    if s.vertex_project:
        return ModelChoice(
            model="vertex_ai/gemini-2.5-flash",
            kwargs={
                "vertex_project": s.vertex_project,
                "vertex_location": s.vertex_location,
            },
        )
    return ModelChoice(
        model="gemini/gemini-2.5-flash",
        kwargs={"api_key": s.google_api_key},
    )
