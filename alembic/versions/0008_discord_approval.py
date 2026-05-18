"""Add Discord approval-message tracking columns.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-28

Replaces the Telegram approval surface with Discord. The host bot
(glitch-discord-bot service) posts an embed in #grow-social per pending
approval; this migration adds the columns we use to remember which
Discord message represents which DB row, so we can edit it on state
change and dispatch the operator's reaction back to the right row.

Telegram columns are LEFT IN PLACE for the duration of the migration
window. They become unused once the new flow is live and can be dropped
in a follow-up migration once we're confident no rollback is needed.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # comment_reply — primary use case (IG comments today, X mentions + LI
    # company-page comments next).
    op.add_column(
        "comment_reply",
        sa.Column("discord_message_id", sa.String(), nullable=True),
    )
    op.add_column(
        "comment_reply",
        sa.Column("discord_channel_id", sa.String(), nullable=True),
    )

    # scheduled_post — the LangGraph telegram_preview node will move here
    # too. Adding now so we don't need a follow-up migration.
    op.add_column(
        "scheduled_post",
        sa.Column("discord_message_id", sa.String(), nullable=True),
    )
    op.add_column(
        "scheduled_post",
        sa.Column("discord_channel_id", sa.String(), nullable=True),
    )

    # mention_event — for the upcoming X mention sweeper.
    op.add_column(
        "mention_event",
        sa.Column("discord_message_id", sa.String(), nullable=True),
    )
    op.add_column(
        "mention_event",
        sa.Column("discord_channel_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mention_event", "discord_channel_id")
    op.drop_column("mention_event", "discord_message_id")
    op.drop_column("scheduled_post", "discord_channel_id")
    op.drop_column("scheduled_post", "discord_message_id")
    op.drop_column("comment_reply", "discord_channel_id")
    op.drop_column("comment_reply", "discord_message_id")
