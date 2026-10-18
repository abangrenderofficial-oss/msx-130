"""MusiX-only Telegram users, even if MediaX shares physical storage."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from handlers import admin
from services.storage.models import Base, User, BotUserRegistry
from services.storage.user_repository import UserRepositoryMixin


class FakeAsyncSession:
    def __init__(self, engine):
        self.engine = engine

    async def __aenter__(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        return self

    async def __aexit__(self, error_type, error, tb):
        if error is None:
            self.transaction.commit()
        else:
            self.transaction.rollback()
        self.connection.close()

    def begin(self):
        class Transaction:
            async def __aenter__(inner):
                return self
            async def __aexit__(inner, *args):
                return False
        return Transaction()

    async def execute(self, statement):
        return self.connection.execute(statement)

    async def scalar(self, statement):
        return self.connection.execute(statement).scalar()


class FakeDatabase(UserRepositoryMixin):
    _dialect_name = "sqlite"
    def __init__(self, engine):
        self.engine = engine
        self._status_cache = {}

    def SessionLocal(self):
        return FakeAsyncSession(self.engine)


@pytest.mark.asyncio
async def test_dedupes_username_changes_and_excludes_mediax_even_with_shared_table():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__, BotUserRegistry.__table__])
    db = FakeDatabase(engine)
    await db.upsert_user(user_id=101, user_name="Ahmad", user_username="ahmad")
    await db.upsert_user(user_id=101, user_name="Ahmad", user_username="ahmad_baru")
    await db.upsert_user(user_id=202, user_name="No username", user_username=None)
    # Telegram group chat identifiers do not represent human users.
    await db.upsert_user(user_id=-10054321, user_name="Group", user_username="group")

    # Simulate a MediaX user stored in exactly the same physical table.
    with engine.begin() as conn:
        conn.execute(
            sqlite_insert(BotUserRegistry).values(
                bot_id="mediax", telegram_user_id=303, username="mediaxuser"
            )
        )
        conn.execute(
            sqlite_insert(BotUserRegistry).values(
                bot_id="mediax", telegram_user_id=101, username="ahmad"
            )
        )
    assert await db.count_musix_unique_users() == 2
    with engine.connect() as conn:
        r = conn.execute(
            select(BotUserRegistry.username).where(
                BotUserRegistry.bot_id == "musix",
                BotUserRegistry.telegram_user_id == 101,
            )
        )
        assert r.scalar() == "ahmad_baru"
        assert conn.execute(select(func.count()).select_from(BotUserRegistry)).scalar() == 4
    engine.dispose()


@pytest.mark.asyncio
async def test_owner_command_reports_only_musix_count(monkeypatch):
    count = AsyncMock(return_value=123)
    monkeypatch.setattr(admin, "db", SimpleNamespace(count_musix_unique_users=count))
    monkeypatch.setattr(admin, "is_admin_music_owner", lambda uid: uid == 999)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )
    await admin.total_musix_unique_users(message)
    count.assert_awaited_once_with()
    result = message.answer.await_args.args[0]
    assert "123" in result
    assert "MusiX" in result
    assert "MediaX" in result


@pytest.mark.asyncio
async def test_nonowner_is_silent_and_never_queries_counts(monkeypatch):
    counter = AsyncMock()
    monkeypatch.setattr(admin, "db", SimpleNamespace(count_musix_unique_users=counter))
    monkeypatch.setattr(admin, "is_admin_music_owner", lambda uid: uid == 999)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=10),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )
    await admin.total_musix_unique_users(message)
    counter.assert_not_awaited()
    message.answer.assert_not_awaited()


def test_command_is_not_in_public_slash_menu():
    from config import BOT_COMMANDS, GROUP_MUSIC_COMMANDS
    assert "totalusermusic" not in {x["command"] for x in BOT_COMMANDS}
    assert "totalusermusic" not in {x["command"] for x in GROUP_MUSIC_COMMANDS}
    assert "total_musix_unique_users" in {
        h.callback.__name__ for h in admin.router.message.handlers
    }
