"""Initial schema — all Glitch Signal tables.

Revision ID: 0001
Revises:
Create Date: 2026-04-16
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("novelty_score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "content_script",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("signal_id", sa.String(), sa.ForeignKey("signal.id"), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("script_body", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("key_visuals", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("shots", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_content_script_signal_id", "content_script", ["signal_id"])

    op.create_table(
        "video_job",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("script_id", sa.String(), sa.ForeignKey("content_script.id"), nullable=False),
        sa.Column("shot_index", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("api_job_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("video_url", sa.String(), nullable=True),
        sa.Column("local_path", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_video_job_script_status", "video_job", ["script_id", "status"])

    op.create_table(
        "video_asset",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("script_id", sa.String(), sa.ForeignKey("content_script.id"), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("qc_notes", sa.Text(), nullable=True),
        sa.Column("assembler_version", sa.String(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("script_id", name="uq_video_asset_script_id"),
    )

    op.create_table(
        "scheduled_post",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("video_asset.id"), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_veto"),
        sa.Column("veto_deadline", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_scheduled_post_status_scheduled_for",
        "scheduled_post",
        ["status", "scheduled_for"],
    )

    op.create_table(
        "published_post",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "scheduled_post_id",
            sa.String(),
            sa.ForeignKey("scheduled_post.id"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("platform_post_id", sa.String(), nullable=False),
        sa.Column("platform_url", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scheduled_post_id", name="uq_published_post_scheduled"),
    )

    op.create_table(
        "metrics_snapshot",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "published_post_id",
            sa.String(),
            sa.ForeignKey("published_post.id"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shares", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_metrics_snapshot_published_post_id",
        "metrics_snapshot",
        ["published_post_id"],
    )

    op.create_table(
        "scout_checkpoint",
        sa.Column("source_key", sa.String(), primary_key=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=False),
        sa.Column("last_ref", sa.String(), nullable=True),
    )

    op.create_table(
        "mention_event",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("mention_id", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("from_handle", sa.String(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=True),
        sa.Column("in_reply_to_id", sa.String(), nullable=True),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("sentiment", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("guardrail_hit", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("mention_id", name="uq_mention_event_mention_id"),
    )
    op.create_index("ix_mention_event_mention_id", "mention_event", ["mention_id"])

    op.create_table(
        "orm_response",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "mention_id", sa.String(), sa.ForeignKey("mention_event.id"), nullable=False
        ),
        sa.Column("draft_body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_review"),
        sa.Column("auto_send_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("sent_by", sa.String(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("mention_id", name="uq_orm_response_mention_id"),
    )
    op.create_index(
        "ix_orm_response_status_auto_send_at",
        "orm_response",
        ["status", "auto_send_at"],
    )


def downgrade() -> None:
    op.drop_table("orm_response")
    op.drop_table("mention_event")
    op.drop_table("scout_checkpoint")
    op.drop_table("metrics_snapshot")
    op.drop_table("published_post")
    op.drop_table("scheduled_post")
    op.drop_table("video_asset")
    op.drop_table("video_job")
    op.drop_table("content_script")
    op.drop_table("signal")
