"""ORM guardrails — pure rule engine, no LLM.

Runs BEFORE the classifier on every inbound mention.
If any hard-stop phrase matches, the mention is flagged and no response
is ever queued, regardless of classifier output.

Hard-stop phrases are loaded from brand.config.json so they can be updated
without a code deploy.
"""
from __future__ import annotations

import re

from glitch_signal.config import brand_config


def check(text: str, brand_id: str | None = None) -> tuple[bool, str | None]:
    """Return (is_safe, hit_phrase).

    is_safe=False means the text triggered a hard-stop rule.
    hit_phrase is the matched phrase (for logging), or None if safe.

    brand_id selects which brand's guardrail list to apply; when omitted,
    the default brand is used (backward-compatible with single-brand callers).
    """
    lower = text.lower()
    cfg = brand_config(brand_id).get("orm_guardrails", {})
    phrases: list[str] = cfg.get("hard_stop_phrases", [])
    for phrase in phrases:
        # Word-boundary-aware match (handles substrings inside words correctly)
        pattern = re.compile(r"\b" + re.escape(phrase.lower()) + r"\b")
        if pattern.search(lower):
            return False, phrase

    # Competitor names (case-insensitive)
    competitors: list[str] = cfg.get("competitor_names", [])
    for name in competitors:
        if name.lower() in lower:
            return False, name

    return True, None
