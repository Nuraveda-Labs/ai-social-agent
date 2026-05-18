"""Add parsed-filename fields to scheduled_post for variant-aware dispatch.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-17

The drive-footage rollout needs the scheduler to pick ScheduledPost rows in a
way that doesn't put near-visual-duplicates back-to-back on the TikTok
grid. Parsing the filename at pick time costs N joins per tick; instead
we denormalise the parsed fields (variant_group, product, geo) onto
ScheduledPost at schedule time and the dispatcher just reads them.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_post", sa.Column("variant_group", sa.String(), nullable=True))
    op.add_column("scheduled_post", sa.Column("product", sa.String(), nullable=True))
    op.add_column("scheduled_post", sa.Column("geo", sa.String(), nullable=True))
    op.create_index(
        "ix_scheduled_post_variant_group",
        "scheduled_post", ["variant_group"], unique=False,
    )
    op.create_index(
        "ix_scheduled_post_brand_status",
        "scheduled_post", ["brand_id", "status"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_post_brand_status", table_name="scheduled_post")
    op.drop_index("ix_scheduled_post_variant_group", table_name="scheduled_post")
    op.drop_column("scheduled_post", "geo")
    op.drop_column("scheduled_post", "product")
    op.drop_column("scheduled_post", "variant_group")
