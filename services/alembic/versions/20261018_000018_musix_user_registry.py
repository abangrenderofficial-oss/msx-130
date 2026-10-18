"""Source-tagged user registry, with backfill only from MusiX's own user table.

Revision ID: 20261018_000018
Revises: 20261018_000017
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_000018"
down_revision: Union[str, Sequence[str], None] = "20261018_000017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "bot_user_registry" not in inspector.get_table_names():
        op.create_table(
            "bot_user_registry",
            sa.Column("bot_id", sa.Text(), nullable=False),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.Text(), nullable=True),
            sa.Column(
                "first_seen_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "last_seen_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("bot_id", "telegram_user_id"),
        )
        op.create_index("ix_bot_user_registry_bot", "bot_user_registry", ["bot_id"])
    # This Alembic migration runs only against MusiX's isolated runtime schema.
    # Existing MusiX users (including those without usernames) count exactly once.
    # Never import or query PayPing / MediaX tables during backfill.
    if "users" in inspector.get_table_names():
        op.execute(sa.text(
            "INSERT INTO bot_user_registry "
            "(bot_id, telegram_user_id, username, first_seen_at, last_seen_at) "
            "SELECT 'musix', user_id, NULLIF(TRIM(user_username), ''), NOW(), NOW() "
            "FROM users WHERE user_id > 0 "
            "ON CONFLICT (bot_id, telegram_user_id) DO NOTHING"
        ))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "bot_user_registry" in inspector.get_table_names():
        op.drop_table("bot_user_registry")
