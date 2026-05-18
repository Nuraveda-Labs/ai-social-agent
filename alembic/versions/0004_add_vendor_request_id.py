"""Add scheduled_post.vendor_request_id for async webhook finalization.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-17

The Upload-Post publisher no longer blocks on get_status polling — it
returns the vendor's request_id immediately and relies on the
upload_completed webhook to finalize the ScheduledPost. We persist
request_id on ScheduledPost so the webhook (and the reconciliation
sweep) can look the row up without scanning.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_post",
        sa.Column("vendor_request_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_scheduled_post_vendor_request_id",
        "scheduled_post",
        ["vendor_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_post_vendor_request_id", table_name="scheduled_post")
    op.drop_column("scheduled_post", "vendor_request_id")
