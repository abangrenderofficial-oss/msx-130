"""Add safe text cleanup ledger for private Musix and connected Music Groups.

Revision ID: 20261019_000019
Revises: 20261018_000018
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261019_000019"
down_revision: Union[str, Sequence[str], None] = "20261018_000018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "music_chat_cleanup_entries" not in inspector.get_table_names():
        op.create_table(
            "music_chat_cleanup_entries",
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_type", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("chat_id", "message_id"),
        )
        op.create_index(
            "ix_music_chat_cleanup_kind_created",
            "music_chat_cleanup_entries",
            ["kind", "created_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "music_chat_cleanup_entries" in inspector.get_table_names():
        op.drop_index(
            "ix_music_chat_cleanup_kind_created",
            table_name="music_chat_cleanup_entries",
        )
        op.drop_table("music_chat_cleanup_entries")
