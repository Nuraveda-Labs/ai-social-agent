"""Thin Discord REST helpers — post + edit approval cards.

We deliberately avoid pulling in `discord.py` here; this process never
opens a gateway. All inbound events come from glitch-discord-bot's file
inbox, all outbound writes go through the REST endpoints below.

Bot token is read from /home/support/.config/glitch-discord/env at
import time — same secret the host bot uses, single source of truth.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

API = "https://discord.com/api/v10"

# Read the same env file the host bot reads. We do this once at import
# time and cache; if the operator rotates the token they restart both.
_HOST_BOT_ENV = Path("/home/support/.config/glitch-discord/env")


def _bot_token() -> str:
    """Resolve the bot token. Prefer process env (already set by the
    host bot when our plugin runs in-bot), fall back to the host-bot
    env file for our own process."""
    tok = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if tok:
        return tok
    if _HOST_BOT_ENV.exists():
        for line in _HOST_BOT_ENV.read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "DISCORD_BOT_TOKEN not in env and host-bot env file is unreadable"
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {_bot_token()}",
        "Content-Type": "application/json",
        "User-Agent": "ai-social-media-agent (+https://github.com/glitch-exec-labs)",
    }


async def post_message(
    channel_id: str,
    *,
    content: str | None = None,
    embeds: list[dict] | None = None,
) -> dict:
    """POST to /channels/{id}/messages. Returns the created message JSON."""
    payload: dict = {}
    if content is not None:
        payload["content"] = content
    if embeds is not None:
        payload["embeds"] = embeds
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{API}/channels/{channel_id}/messages",
            headers=_headers(), json=payload,
        )
    if r.status_code >= 400:
        log.warning("discord.rest.post_failed", status=r.status_code, body=r.text[:300])
        r.raise_for_status()
    return r.json()


async def edit_message(
    channel_id: str,
    message_id: str,
    *,
    content: str | None = None,
    embeds: list[dict] | None = None,
) -> dict:
    """PATCH a previously-posted message — used to update the embed when
    the underlying row's state changes (e.g. pending -> posted)."""
    payload: dict = {}
    if content is not None:
        payload["content"] = content
    if embeds is not None:
        payload["embeds"] = embeds
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.patch(
            f"{API}/channels/{channel_id}/messages/{message_id}",
            headers=_headers(), json=payload,
        )
    if r.status_code >= 400:
        log.warning("discord.rest.edit_failed", status=r.status_code, body=r.text[:300])
        r.raise_for_status()
    return r.json()


async def add_reaction(
    channel_id: str, message_id: str, emoji: str,
) -> None:
    """PUT a bot reaction onto a message — used to seed the approval keys
    (✅ / ❌) on every embed so operators can click instead of typing.

    Discord rate-limits reactions at ~1 per 0.3s per message. We retry
    once on 429 honoring the server-supplied retry_after.
    """
    import asyncio
    from urllib.parse import quote

    e = quote(emoji, safe="")
    url = f"{API}/channels/{channel_id}/messages/{message_id}/reactions/{e}/@me"
    async with httpx.AsyncClient(timeout=20) as c:
        for _attempt in range(3):
            r = await c.put(url, headers=_headers())
            if r.status_code < 400:
                return
            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("retry_after", 0.5))
                except (ValueError, TypeError):
                    retry_after = 0.5
                await asyncio.sleep(min(retry_after + 0.05, 2.0))
                continue
            break
    log.warning(
        "discord.rest.reaction_failed",
        status=r.status_code, body=r.text[:300], emoji=emoji,
    )


def approver_id_check(user_id: int) -> bool:
    """Convenience reexport for callers that don't want to import auth."""
    from glitch_signal.discord.auth import is_approver
    return is_approver(user_id)


# ---------------------------------------------------------------------------
# Helpers for callers that read interaction events from the host bot's
# file inbox at /home/support/.glitch-discord/inbox/_interactions/. Each
# JSON file there is one button click; the host bot writes them, agents
# consume + delete.
# ---------------------------------------------------------------------------

INTERACTION_INBOX = Path("/home/support/.glitch-discord/inbox/_interactions/")


def consume_interactions(
    *, prefix: str = "act:",
) -> list[dict]:
    """Read & remove every interaction file whose custom_id starts with
    `prefix`. Caller is expected to dispatch the contained action."""
    if not INTERACTION_INBOX.exists():
        return []
    out: list[dict] = []
    for path in sorted(INTERACTION_INBOX.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        custom_id = data.get("custom_id", "") or data.get("data", {}).get("custom_id", "")
        if not custom_id.startswith(prefix):
            continue
        try:
            path.unlink()
        except OSError:
            pass
        out.append(data)
    return out
