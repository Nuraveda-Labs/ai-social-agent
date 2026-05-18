"""ORM classifier — LLM-based tier classification for inbound mentions.

Tiers:
  positive        → auto-respond immediately
  neutral_faq     → auto-respond with link + answer
  neutral_technical → open GitHub issue, reply with link
  negative_mild   → draft + 2h Telegram review window
  negative_severe → Telegram alert only, no response
  legal_flag      → Telegram alert only, no response ever
  spam            → ignore

If confidence < threshold (default 0.7), falls back to negative_severe.
"""
from __future__ import annotations

import json

import litellm
import structlog

from social_signal.agent.llm import pick
from social_signal.config import brand_config, settings

log = structlog.get_logger(__name__)

VALID_TIERS = {
    "positive",
    "neutral_faq",
    "neutral_technical",
    "negative_mild",
    "negative_severe",
    "legal_flag",
    "spam",
}

_SYSTEM_PROMPT = """You are a social media ORM classifier for this project — an algorithmic trading AI platform.

Classify the inbound mention into one of these tiers:
- positive: genuine praise, thanks, enthusiasm about the product/team
- neutral_faq: question about pricing, features, how to use it, where to find docs
- neutral_technical: bug report, technical question that belongs in a GitHub issue
- negative_mild: mild complaint, frustration, disappointment (non-threatening)
- negative_severe: strong negative, threats, demands, public call-out
- legal_flag: mentions of SEC, SEBI, FINRA, returns guarantees, legal action, lawsuit, investment advice
- spam: obvious spam, irrelevant, bots, solicitations

Respond JSON only:
{
  "tier": "...",
  "sentiment": "positive | neutral | negative",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence"
}
"""


async def classify(
    mention_body: str,
    platform: str,
    brand_id: str | None = None,
) -> dict:
    """Classify a mention. Returns dict with tier, sentiment, confidence, reasoning.

    brand_id selects which brand's min_confidence_threshold applies.
    """
    min_confidence = (
        brand_config(brand_id)
        .get("orm_guardrails", {})
        .get("min_confidence_threshold", 0.7)
    )

    if settings().is_dry_run:
        return {
            "tier": "positive",
            "sentiment": "positive",
            "confidence": 1.0,
            "reasoning": "dry-run classification",
        }

    mc = pick("smart")
    try:
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Platform: {platform}\nMention: {mention_body[:500]}",
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=150,
            **mc.kwargs,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as exc:
        log.warning("classifier.failed", error=str(exc))
        return {
            "tier": "negative_severe",
            "sentiment": "negative",
            "confidence": 0.0,
            "reasoning": f"classifier error: {exc}",
        }

    tier = str(data.get("tier", "negative_severe")).lower()
    confidence = float(data.get("confidence", 0.0))

    if tier not in VALID_TIERS:
        tier = "negative_severe"

    # Conservative fallback: low confidence → treat as negative_severe
    if confidence < min_confidence and tier not in ("spam", "positive"):
        log.info(
            "classifier.confidence_fallback",
            original_tier=tier,
            confidence=confidence,
        )
        tier = "negative_severe"
        confidence = 0.0

    return {
        "tier": tier,
        "sentiment": str(data.get("sentiment", "neutral")),
        "confidence": confidence,
        "reasoning": str(data.get("reasoning", "")),
    }
