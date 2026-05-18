"""Render approval embeds for Discord.

One function per row type. Each returns a `dict` (Discord embed JSON)
rather than a `discord.Embed` object so this module is import-safe in
contexts that don't have `discord.py` available — same module is used
both inside the host bot's process AND from this repo's REST poster.
"""
from __future__ import annotations

from datetime import UTC, datetime

from glitch_signal.config import brand_config
from glitch_signal.db.models import CommentReply

# Discord limits — embed.description hard cap is 4096; budget for fences.
_DESCRIPTION_BUDGET = 3800

_STATE_COLOR = {
    "pending_approval": 0x3498DB,  # blue
    "drafted":          0x3498DB,
    "posted":           0x2ECC71,  # green
    "ignored":          0x95A5A6,  # gray
    "failed":           0xE74C3C,  # red
}


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def comment_reply_embed(row: CommentReply, *, state_override: str | None = None) -> dict:
    """Embed for a CommentReply row pending approval. Same shape as the
    sales-agent draft_embed so operators have one consistent UX.
    """
    state = state_override or row.status
    color = _STATE_COLOR.get(state, 0x3498DB)
    display = brand_config(row.brand_id).get("display_name", row.brand_id)
    platform_label = row.platform.replace("upload_post_", "").upper()

    drafted = row.drafted_reply or "(no draft yet)"
    body_block = (
        f"**Their comment**\n"
        f"```\n{_truncate(row.comment_text, 1500)}\n```\n"
        f"**Drafted reply**\n"
        f"```\n{_truncate(drafted, 1500)}\n```"
    )
    description = _truncate(body_block, _DESCRIPTION_BUDGET)

    commenter = row.commenter_handle or row.commenter_name or "anon"
    embed: dict = {
        "title": f"[{display}] {platform_label} comment",
        "description": description,
        "color": color,
        "timestamp": datetime.now(UTC).isoformat(),
        "fields": [
            {"name": "From",     "value": commenter,             "inline": True},
            {"name": "Tier",     "value": row.triage_tier or "?", "inline": True},
            {"name": "Platform", "value": platform_label,         "inline": True},
        ],
        "footer": {"text": f"comment_reply {row.id} · ✅ send · ❌ skip"},
    }
    return embed
