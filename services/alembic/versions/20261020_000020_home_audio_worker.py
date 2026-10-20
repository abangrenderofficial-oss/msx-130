"""Add opt-in Musix home audio queue and worker heartbeat tables.

Revision ID: 20261020_000020
Revises: 20261019_000019
"""
from alembic import op
import sqlalchemy as sa

revision = "20261020_000020"
down_revision = "20261019_000019"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = inspector.get_table_names()
    if "home_audio_jobs" not in existing:
        op.create_table(
            "home_audio_jobs",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("video_id", sa.Text(), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("request_message_id", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("worker_id", sa.Text()),
            sa.Column("claim_token", sa.Text()),
            sa.Column("file_id", sa.Text()),
            sa.Column("title", sa.Text()),
            sa.Column("performer", sa.Text()),
            sa.Column("duration_seconds", sa.Float()),
            sa.Column("failure_reason", sa.Text()),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
            sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True)),
            sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        )
        op.create_index("ix_home_audio_jobs_pending", "home_audio_jobs", ["status", "created_at"])
        op.create_index("ix_home_audio_jobs_stats", "home_audio_jobs", ["created_at", "status"])
    if "home_audio_heartbeats" not in existing:
        op.create_table(
            "home_audio_heartbeats",
            sa.Column("worker_id", sa.Text(), primary_key=True),
            sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "home_audio_heartbeats" in inspector.get_table_names():
        op.drop_table("home_audio_heartbeats")
    if "home_audio_jobs" in inspector.get_table_names():
        op.drop_index("ix_home_audio_jobs_stats", table_name="home_audio_jobs")
        op.drop_index("ix_home_audio_jobs_pending", table_name="home_audio_jobs")
        op.drop_table("home_audio_jobs")
