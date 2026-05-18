"""Make asset_id nullable + add direct script_id link for text-only posts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-20

The ai_generated text pipeline produces ContentScript rows directly (text posts
for LinkedIn/X) without going through the video generation/assembly chain, so
these ScheduledPost rows have no backing VideoAsset. Two schema changes to
support this:

  1. ScheduledPost.asset_id → nullable. Text posts leave it NULL.
  2. New nullable ScheduledPost.script_id column with a FK to content_script,
     so the publisher can resolve the post body from the ContentScript
     directly without a VideoAsset round-trip.

Existing video rows keep asset_id populated and leave script_id NULL; the
publisher/scheduler routes on asset_id vs script_id presence.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "scheduled_post",
        "asset_id",
        existing_type=sa.String(),
        nullable=True,
    )
    op.add_column(
        "scheduled_post",
        sa.Column("script_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scheduled_post_script_id",
        "scheduled_post",
        "content_script",
        ["script_id"],
        ["id"],
    )
    op.create_index(
        "ix_scheduled_post_script_id",
        "scheduled_post",
        ["script_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_post_script_id", table_name="scheduled_post")
    op.drop_constraint("fk_scheduled_post_script_id", "scheduled_post", type_="foreignkey")
    op.drop_column("scheduled_post", "script_id")
    op.alter_column(
        "scheduled_post",
        "asset_id",
        existing_type=sa.String(),
        nullable=False,
    )
