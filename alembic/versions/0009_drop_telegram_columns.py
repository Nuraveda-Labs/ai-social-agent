"""Drop telegram_message_id columns; add discord columns to orm_response.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-01

The Telegram approval surface was retired 2026-05-01. All HITL flows
land in #grow-social on the host glitch-discord-bot service. Migration
0008 added discord_message_id / discord_channel_id to comment_reply,
scheduled_post, and mention_event. This one:

  - drops telegram_message_id from orm_response (the only table where
    it was still being read after 0008 — comment_reply and friends
    used it but those code paths were already migrated)
  - adds discord_message_id + discord_channel_id to orm_response so
    the host-bot plugin can track ORM review embeds the same way it
    tracks comment-reply embeds.

We leave telegram_message_id ON comment_reply / scheduled_post /
mention_event for one more cycle (data is harmless, dropping it can
wait until we're confident no rollback is needed).
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("orm_response", "telegram_message_id")
    op.add_column(
        "orm_response",
        sa.Column("discord_message_id", sa.String(), nullable=True),
    )
    op.add_column(
        "orm_response",
        sa.Column("discord_channel_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orm_response", "discord_channel_id")
    op.drop_column("orm_response", "discord_message_id")
    op.add_column(
        "orm_response",
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
    )
