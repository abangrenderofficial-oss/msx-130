"""Count successful private audio requests for the admin monitor.

Revision ID: 20261018_000017
Revises: 20261009_000016
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_000017"
down_revision: Union[str, Sequence[str], None] = "20261009_000016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "admin_private_audio_usage" not in inspector.get_table_names():
        op.create_table(
            "admin_private_audio_usage",
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("request_message_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("user_id", "request_message_id"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "admin_private_audio_usage" in inspector.get_table_names():
        op.drop_table("admin_private_audio_usage")
