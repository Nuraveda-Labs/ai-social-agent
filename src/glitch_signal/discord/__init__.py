"""Discord HITL approval surface — replacement for the Telegram bot.

The Discord control plane lives in a separate repo (glitch-discord-bot)
running as a systemd service on the same box. That bot owns the gateway
WebSocket; we never connect to Discord directly from this process.

Two interaction directions:

  Out (this repo  -> Discord channel):
    discord.rest.send_approval_card(row)   posts an embed via REST,
                                            stores message_id on the row.
    discord.rest.update_approval_card(row) edits the embed when state
                                            changes (e.g. posted, vetoed).

  In  (Discord    -> this repo):
    The host bot watches reactions on every message it cached and writes
    a JSON event to /home/support/.glitch-discord/inbox/_interactions/.
    A small plugin loaded INTO the host bot (registered at deploy time)
    polls our DB for pending approvals and dispatches the operator's
    reaction back to comments.sweeper.approve_reply / veto_reply.

This module owns the formatting + REST plumbing only. The polling
process lives in glitch-discord-bot/social_media_agent_integration.py
because it has to share the host bot's discord.py client.
"""
