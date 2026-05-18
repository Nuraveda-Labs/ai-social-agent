"""BSK-004 Social → brain bridge (GROW-BIND-3, 2026-05-17).

Mirrors every successful publish on the Social agent onto the
shared `brain-mcp` so sibling agents on the same brand can
see what Social just published via `team_state` /
`recent_activity` / `briefing`.

This is **additive**: the existing `PublishedPost` row in the
`signal` Postgres database stays as the agent's primary record of
what got published. The brain mirror is the sibling-visible
coordination layer.

Wiring contract (BIND-1b multi-brand pattern):
  - Env `BRAIN_MCP_URL` overrides the brain URL.
  - Per-brand tokens: `BRAIN_TOKEN_BSK_004_<BRAND_SLUG_UPPER>` (e.g.
    `BRAIN_TOKEN_BSK_004_GLITCH_EXECUTOR`, `BRAIN_TOKEN_BSK_004_example`).
    Bridge resolves the right one from the `brand_id` field on the
    PublishedPost row.
  - Backward-compat fallback: bare `BRAIN_TOKEN_BSK_004` if it's set
    (single-brand dev setups only).
  - Unknown / None brand → silent no-op.
  - All brain calls are fire-and-forget; brain failures NEVER block
    or fail the local insert.

Per the brands × agents matrix (`brands_agent_matrix.md`), BSK-004
Social is enrolled in `glitch-executor` and `example` — both have
brain tokens in the consolidated `.env`.

Note on the brand-slug normalization: Social's model layer stores
brand_id in either snake form (`glitch_executor`) or kebab form
(`example`). The normalizer accepts both; the env-key suffix is
always UPPER_SNAKE.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from grow_platform.brain import BrainAuthError, BrainClient, BrainError
from grow_platform.brain.limits import cap_payload

log = logging.getLogger(__name__)

_DEFAULT_BRAIN_URL = "http://127.0.0.1:3107/mcp"
_BRAIN_TOKEN_PREFIX = "BRAIN_TOKEN_BSK_004_"
_BRAIN_TOKEN_LEGACY = "BRAIN_TOKEN_BSK_004"
_BRAIN_URL_ENV = "BRAIN_MCP_URL"

_NON_ENV_CHARS = re.compile(r"[^A-Z0-9_]")


def _slug_to_env_suffix(brand_id: str | None) -> str | None:
    """Normalize a brand identifier to its env-key suffix.

    Accepts both snake (`glitch_executor`) and kebab (`urban-classics`)
    forms — both convert to UPPER_SNAKE (`GLITCH_EXECUTOR`,
    `URBAN_CLASSICS`)."""
    if not brand_id:
        return None
    s = brand_id.strip().upper().replace("-", "_")
    s = _NON_ENV_CHARS.sub("", s)
    return s or None


def _brain_token_for(brand_id: str | None) -> str | None:
    suffix = _slug_to_env_suffix(brand_id)
    if suffix is not None:
        per_brand = os.environ.get(_BRAIN_TOKEN_PREFIX + suffix)
        if per_brand:
            return per_brand
    legacy = os.environ.get(_BRAIN_TOKEN_LEGACY)
    return legacy or None


def _brain_url() -> str:
    return os.environ.get(_BRAIN_URL_ENV, _DEFAULT_BRAIN_URL)


def brain_available_for(brand_id: str | None) -> bool:
    """True when a BSK-004 token resolves for this brand."""
    return _brain_token_for(brand_id) is not None


def brain_available() -> bool:
    """True when ANY BSK-004 token is configured (any per-brand OR the
    legacy bare key). Used as a global diagnostic on startup."""
    if any(k.startswith(_BRAIN_TOKEN_PREFIX) and os.environ[k] for k in os.environ):
        return True
    return bool(os.environ.get(_BRAIN_TOKEN_LEGACY))


def _summarize_for_brain(text: str | None, max_chars: int = 240) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rstrip() + "…"


async def mirror_published_post_to_brain(
    *,
    brand_id: str | None,
    platform: str,
    platform_post_id: str | None,
    platform_url: str | None,
    scheduled_post_id: str | None,
) -> None:
    """Best-effort mirror of one PublishedPost insert to the brain.

    Called from `scheduler/queue.py` after a successful publish-side
    commit. Errors caught + logged at WARNING; the local DB record
    is never rolled back on brain failure.
    """
    token = _brain_token_for(brand_id)
    if token is None:
        return

    summary_parts: list[str] = [f"published on {platform}"]
    if platform_url:
        summary_parts.append(platform_url)
    elif platform_post_id:
        summary_parts.append(f"post_id={platform_post_id}")

    try:
        async with BrainClient(url=_brain_url(), token=token) as brain:
            await brain.append_activity(
                action="social.published",
                summary=_summarize_for_brain(" — ".join(summary_parts)),
                subject=platform,
                payload=cap_payload({
                    "platform": platform,
                    "platform_post_id": platform_post_id,
                    "platform_url": platform_url,
                    "scheduled_post_id": scheduled_post_id,
                }),
                agent_sku="BSK-004",
            )
    except BrainAuthError:
        suffix = _slug_to_env_suffix(brand_id) or "<unknown>"
        log.warning(
            "social brain mirror auth failed for brand_id=%r (BSK-004); "
            "check env var %s%s (or legacy %s)",
            brand_id, _BRAIN_TOKEN_PREFIX, suffix, _BRAIN_TOKEN_LEGACY,
        )
    except BrainError as e:
        log.warning("social brain mirror failed: %s", e)
    except Exception:  # noqa: BLE001 — never let brain take down the agent
        log.exception("social brain mirror raised unexpectedly")


def schedule_published_post_mirror(
    *,
    brand_id: str | None,
    platform: str,
    platform_post_id: str | None,
    platform_url: str | None,
    scheduled_post_id: str | None,
) -> None:
    """Schedule a brain mirror on the running event loop.

    Call this right after the local `session.commit()` that persists
    a `PublishedPost` row. Mirrors the fire-and-forget convention so
    brain mirroring never delays the publish-side response.
    """
    if not brain_available_for(brand_id):
        return
    asyncio.ensure_future(
        mirror_published_post_to_brain(
            brand_id=brand_id,
            platform=platform,
            platform_post_id=platform_post_id,
            platform_url=platform_url,
            scheduled_post_id=scheduled_post_id,
        )
    )
