"""Add platform_auth table for OAuth token custody.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-17

Tokens are encrypted at rest (Fernet) using AUTH_ENCRYPTION_KEY. The table
stores one row per (brand_id, platform, account_identifier) tuple.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_auth",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("account_identifier", sa.String(), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("raw_provider_response", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "brand_id", "platform", "account_identifier",
            name="uq_platform_auth_brand_platform_account",
        ),
    )
    op.create_index("ix_platform_auth_brand_id", "platform_auth", ["brand_id"])
    op.create_index("ix_platform_auth_platform", "platform_auth", ["platform"])
    op.create_index(
        "ix_platform_auth_brand_platform",
        "platform_auth",
        ["brand_id", "platform"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_auth_brand_platform", table_name="platform_auth")
    op.drop_index("ix_platform_auth_platform", table_name="platform_auth")
    op.drop_index("ix_platform_auth_brand_id", table_name="platform_auth")
    op.drop_table("platform_auth")
