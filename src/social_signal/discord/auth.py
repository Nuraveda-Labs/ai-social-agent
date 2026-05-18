"""Approver allowlist for Discord HITL actions.

Single source of truth = the host bot's DISCORD_APPROVER_USER_IDS_JSON
env var (set in /home/support/.config/glitch-discord/env). When the
plugin runs inside the host bot's process, that env is already loaded.
We also accept a comma-separated fallback for direct invocation in
tests / one-off scripts.
"""
from __future__ import annotations

import json
import os


def approver_user_ids() -> list[int]:
    """Return the int list of Discord user IDs allowed to approve."""
    raw = os.environ.get("DISCORD_APPROVER_USER_IDS_JSON", "").strip()
    if raw:
        try:
            ids = json.loads(raw)
            return [int(x) for x in ids]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    raw = os.environ.get("DISCORD_APPROVER_USER_IDS", "").strip()
    if raw:
        return [int(x) for x in raw.split(",") if x.strip()]
    return []


def is_approver(user_id: int) -> bool:
    return user_id in approver_user_ids()
