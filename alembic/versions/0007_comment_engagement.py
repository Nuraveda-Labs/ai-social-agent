"""Add comment_reply + strategic_reply tables for engagement automation.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-21

Two new tables supporting the engagement layer on top of the publishing
pipeline:

  comment_reply    — one row per incoming comment on our own posts. The
                     sweeper fetches comments via Upload-Post get_post_comments,
                     drafts a reply in brand voice, queues it for Telegram
                     approval, and posts via reply_to_comment.

  strategic_reply  — one row per "the operator wants to reply to someone
                     else's post". Covers the 70/30 growth pattern (most
                     growth comes from value-adding replies to larger
                     accounts). For X we can auto-post via upload_text +
                     quote_tweet_id; for LinkedIn we hand the drafted reply
                     back to the operator as copy-ready text.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comment_reply",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), nullable=False, index=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("published_post_id", sa.String(), nullable=True),
        sa.Column("platform_post_id", sa.String(), nullable=False, index=True),
        sa.Column("platform_comment_id", sa.String(), nullable=False, unique=True),
        sa.Column("commenter_handle", sa.String(), nullable=True),
        sa.Column("commenter_name", sa.String(), nullable=True),
        sa.Column("comment_text", sa.Text(), nullable=False),
        sa.Column("comment_created_at", sa.DateTime(), nullable=True),
        sa.Column("triage_tier", sa.String(), nullable=True),
        # new | drafted | pending_approval | posted | ignored | failed
        sa.Column("status", sa.String(), nullable=False, default="new", index=True),
        sa.Column("drafted_reply", sa.Text(), nullable=True),
        sa.Column("posted_reply_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "strategic_reply",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), nullable=False, index=True),
        sa.Column("target_platform", sa.String(), nullable=False),
        # x | linkedin | unknown
        sa.Column("target_post_url", sa.String(), nullable=False),
        sa.Column("target_post_id", sa.String(), nullable=True),
        sa.Column("target_author_handle", sa.String(), nullable=True),
        sa.Column("target_post_text", sa.Text(), nullable=True),
        sa.Column("drafted_reply", sa.Text(), nullable=True),
        # new | drafted | pending_approval | posted | copied | vetoed | failed
        sa.Column("status", sa.String(), nullable=False, default="new", index=True),
        sa.Column("requested_by_telegram_id", sa.String(), nullable=True),
        sa.Column("posted_platform_post_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("strategic_reply")
    op.drop_table("comment_reply")
