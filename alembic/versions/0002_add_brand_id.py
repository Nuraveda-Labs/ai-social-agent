"""Add brand_id column + index to every brand-scoped table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-17

All existing rows are backfilled to the default brand ('glitch_executor'),
then the server_default is dropped so new rows must specify brand_id
explicitly. This is the foundation for multi-brand support introduced
alongside the multi-brand onboarding (PR #1).
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DEFAULT_BRAND = "glitch_executor"

# (table_name, index_name) — every table that carries brand-scoped content.
_BRAND_TABLES: list[tuple[str, str]] = [
    ("signal",            "ix_signal_brand_id"),
    ("content_script",    "ix_content_script_brand_id"),
    ("video_job",         "ix_video_job_brand_id"),
    ("video_asset",       "ix_video_asset_brand_id"),
    ("scheduled_post",    "ix_scheduled_post_brand_id"),
    ("published_post",    "ix_published_post_brand_id"),
    ("metrics_snapshot",  "ix_metrics_snapshot_brand_id"),
    ("scout_checkpoint",  "ix_scout_checkpoint_brand_id"),
    ("mention_event",     "ix_mention_event_brand_id"),
    ("orm_response",      "ix_orm_response_brand_id"),
]


def upgrade() -> None:
    for table, index in _BRAND_TABLES:
        # 1. Add column with a server_default so existing rows get the default brand.
        op.add_column(
            table,
            sa.Column(
                "brand_id",
                sa.String(),
                nullable=False,
                server_default=DEFAULT_BRAND,
            ),
        )
        # 2. Explicit backfill (belt-and-braces — server_default already handled it).
        op.execute(
            sa.text(
                f"UPDATE {table} SET brand_id = :b WHERE brand_id IS NULL OR brand_id = ''"
            ).bindparams(b=DEFAULT_BRAND)
        )
        # 3. Drop the server_default so new rows must be explicit.
        op.alter_column(table, "brand_id", server_default=None)
        # 4. Index for filtering.
        op.create_index(index, table, ["brand_id"])


def downgrade() -> None:
    for table, index in reversed(_BRAND_TABLES):
        op.drop_index(index, table_name=table)
        op.drop_column(table, "brand_id")
