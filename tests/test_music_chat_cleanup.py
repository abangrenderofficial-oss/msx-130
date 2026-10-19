"""Safety regression tests: manual duplicate cleanup vs hourly text-only cleanup."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import music_chat_cleanup
from handlers import private_music, group_music


def _track(message_id, *, source="https://youtu.be/Ftffph3fVEs", title="Song"):
    return SimpleNamespace(
        audio_message_id=message_id,
        service="youtube",
        source_url=source,
        title=title,
        performer="Artist",
        telegram_file_id="telegram-same-file",
    )


def test_duplicate_audio_ids_keep_oldest_track_only():
    assert music_chat_cleanup.duplicate_audio_message_ids(
        [_track(30), _track(22), _track(24), _track(40, source="other", title="Other")]
    ) == [24, 30]


def test_duplicate_cleanup_does_not_pick_unique_audio():
    tracks = [_track(11), _track(25, source="https://youtu.be/dQw4w9WgXcQ", title="Different")]
    assert music_chat_cleanup.duplicate_audio_message_ids(tracks) == []


@pytest.mark.asyncio
async def test_cleanduplicate_only_deletes_duplicate_audio_and_records_tombstone(monkeypatch):
    bot = SimpleNamespace(
        delete_messages=AsyncMock(return_value=True),
        delete_message=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_media=AsyncMock(),
    )
    monkeypatch.setattr(music_chat_cleanup, "_bot", lambda: bot)
    monkeypatch.setattr(music_chat_cleanup, "active_audio_tracks", AsyncMock(side_effect=lambda _id, rows: list(rows)))
    mark = AsyncMock()
    monkeypatch.setattr(music_chat_cleanup, "mark_deleted_audio", mark)
    monkeypatch.setattr(music_chat_cleanup.asyncio, "sleep", AsyncMock())

    removed, failed = await music_chat_cleanup.remove_only_duplicate_audio(
        42, "private", [_track(7), _track(10), _track(9)],
    )
    assert (removed, failed) == (2, 0)
    bot.delete_messages.assert_awaited_once_with(chat_id=42, message_ids=[9, 10])
    bot.delete_message.assert_not_awaited()
    bot.edit_message_caption.assert_not_awaited()
    bot.edit_message_media.assert_not_awaited()
    mark.assert_awaited_once_with(42, "private", {9, 10})


@pytest.mark.asyncio
async def test_private_cleanduplicate_uses_user_playlist_and_no_text_cleanup(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42),
        reply=AsyncMock(),
    )
    rows = [_track(7), _track(9)]
    list_tracks = AsyncMock(return_value=rows)
    remove = AsyncMock(return_value=(1, 0))
    monkeypatch.setattr(private_music, "list_private_tracks_raw", list_tracks)
    monkeypatch.setattr(private_music, "remove_only_duplicate_audio", remove)
    await private_music.clean_duplicate_private_music(message)
    list_tracks.assert_awaited_once_with(42)
    remove.assert_awaited_once_with(42, "private", rows)
    message.reply.assert_awaited_once()
    assert "/cleanduplicate" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_group_cleanduplicate_restricted_to_connected_group_admin(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-10042, type="supergroup"),
        message_id=77,
        from_user=SimpleNamespace(id=9),
        reply=AsyncMock(return_value=SimpleNamespace(message_id=78)),
    )
    db = SimpleNamespace(list_music_group_tracks_raw=AsyncMock(return_value=[_track(10)]))
    monkeypatch.setattr(group_music, "db", db)
    monkeypatch.setattr(group_music, "_remember_command", AsyncMock())
    monkeypatch.setattr(group_music, "_require_group_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(group_music, "_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(group_music, "_reply_tracked", AsyncMock())
    remover = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr(group_music, "remove_only_duplicate_audio", remover)
    await group_music.clean_duplicate_group_music(message)
    remover.assert_awaited_once()
    assert remover.await_args.args[0:2] == (-10042, "group")

    monkeypatch.setattr(group_music, "_connected", AsyncMock(return_value=False))
    remover.reset_mock()
    await group_music.clean_duplicate_group_music(message)
    remover.assert_not_awaited()

    monkeypatch.setattr(group_music, "_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(group_music, "_require_group_admin", AsyncMock(return_value=False))
    await group_music.clean_duplicate_group_music(message)
    remover.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_tracker_never_tracks_media_caption(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(music_chat_cleanup, "_save_entry", save)
    chat = SimpleNamespace(id=42, type="private")
    media = SimpleNamespace(
        text=None,
        caption="https://youtu.be/Ftffph3fVEs",
        chat=chat,
        message_id=10,
        from_user=SimpleNamespace(is_bot=False),
    )
    await music_chat_cleanup.track_text_message(media)
    assert save.await_count == 0

    text_message = SimpleNamespace(
        text="https://youtu.be/Ftffph3fVEs",
        chat=chat,
        message_id=11,
        from_user=SimpleNamespace(is_bot=False),
    )
    await music_chat_cleanup.track_text_message(text_message)
    save.assert_awaited_once_with(42, 11, "private", "text")


@pytest.mark.asyncio
async def test_daily_auto_cleanup_never_acts_on_admin_monitor_group(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [
                (-1005, 91, "group"),  # Admin-monitor group: must be skipped
                (42, 92, "private"),
            ])
    monkeypatch.setattr(
        music_chat_cleanup,
        "_database",
        lambda: SimpleNamespace(SessionLocal=lambda: Session()),
    )
    checker = AsyncMock(return_value=False)
    monkeypatch.setattr(music_chat_cleanup, "_allowed_group", checker)
    deletions = AsyncMock(return_value=({92}, set()))
    monkeypatch.setattr(music_chat_cleanup, "_delete_ids", deletions)
    deleted_rows = AsyncMock()
    monkeypatch.setattr(music_chat_cleanup, "remove_text_entries", deleted_rows)
    await music_chat_cleanup.auto_clear_text_after_24h()
    checker.assert_awaited_once_with(-1005)
    deletions.assert_awaited_once()
    assert deletions.await_args.args[1:] == (42, [92])
    deleted_rows.assert_awaited_once_with(42, {92})
