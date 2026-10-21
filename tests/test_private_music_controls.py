from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import private_music
from services import private_music_playlist


def _track(
    message_id: int,
    *,
    title: str = "Example Song",
    performer: str = "Example Artist",
    source_url: str = "https://youtu.be/Ftffph3fVEs",
    file_id: str = "file-id",
):
    return SimpleNamespace(
        id=message_id,
        service="youtube",
        source_url=source_url,
        title=title,
        performer=performer,
        telegram_file_id=file_id,
        duration_seconds=200,
        source_message_id=message_id - 1,
        audio_message_id=message_id,
    )


@pytest.mark.asyncio
async def test_private_clearall_preserves_all_audio_including_duplicates(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        message_id=30,
    )
    first = _track(20, file_id="file-a")
    duplicate = _track(22, file_id="file-b")

    delete_messages = AsyncMock(
        return_value=({19, 21, 30}, set())
    )
    clean_audio = AsyncMock()

    monkeypatch.setattr(
        private_music,
        "list_private_tracks_raw",
        AsyncMock(return_value=[first, duplicate]),
    )
    monkeypatch.setattr(
        private_music,
        "get_private_source_message_ids",
        AsyncMock(return_value={19, 21}),
    )
    monkeypatch.setattr(
        private_music,
        "delete_private_messages",
        delete_messages,
    )
    monkeypatch.setattr(
        private_music, "get_text_message_ids",
        AsyncMock(return_value={19, 21}),
    )
    monkeypatch.setattr(private_music, "remove_text_entries", AsyncMock())
    monkeypatch.setattr(private_music, "_CLEARALL_SWEEP_LIMIT", 50)

    await private_music.clear_all_private_music(message)

    target_ids = delete_messages.await_args.args[1]
    assert 20 not in target_ids
    assert 22 not in target_ids
    assert 19 in target_ids
    assert 21 in target_ids
    assert 30 in target_ids
    clean_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_clearall_uses_only_verified_text_and_no_history_probe(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        message_id=128,
    )
    delete_messages = AsyncMock(return_value=({120, 121, 122, 128}, set()))

    monkeypatch.setattr(
        private_music,
        "list_private_tracks_raw",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        private_music,
        "get_private_source_message_ids",
        AsyncMock(return_value={120}),
    )
    monkeypatch.setattr(
        private_music,
        "delete_private_messages",
        delete_messages,
    )

    probe_mock = AsyncMock()
    monkeypatch.setattr(private_music, "_probe_private_message_has_audio", probe_mock)
    monkeypatch.setattr(
        private_music, "get_text_message_ids",
        AsyncMock(return_value={121, 122, 124}),
    )
    monkeypatch.setattr(private_music, "remove_text_entries", AsyncMock())
    monkeypatch.setattr(private_music, "_LEGACY_PROBE_LIMIT", 8)
    monkeypatch.setattr(private_music.asyncio, "sleep", AsyncMock())

    await private_music.clear_all_private_music(message)

    target_ids = delete_messages.await_args.args[1]
    assert target_ids == {121, 122, 124, 128}
    assert 120 not in target_ids  # unverified source may be media caption
    probe_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_private_audio_keeps_new_duplicate_and_tracks_it(monkeypatch):
    existing = _track(20, file_id="file-old")
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        message_id=21,
    )
    fake_bot = SimpleNamespace(delete_message=AsyncMock())

    monkeypatch.setattr(private_music_playlist, "bot", fake_bot)
    monkeypatch.setattr(
        private_music_playlist,
        "list_private_tracks_raw",
        AsyncMock(return_value=[existing]),
    )
    add_remote = AsyncMock()
    monkeypatch.setattr(
        private_music_playlist,
        "add_remote_music_group_track",
        add_remote,
    )

    stored = await private_music_playlist.record_private_audio(
        message,
        service="youtube",
        source_url=existing.source_url,
        file_id="file-new",
        audio_message_id=22,
        title=existing.title,
        performer=existing.performer,
        duration=200,
    )

    assert stored is True
    fake_bot.delete_message.assert_not_awaited()
    add_remote.assert_awaited_once()
    payload = add_remote.await_args.kwargs
    assert payload["audio_message_id"] == 22
    assert payload["source_message_id"] == 21
    assert payload["source_url"] == existing.source_url


@pytest.mark.asyncio
async def test_record_private_audio_persists_new_track(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=4321, type="private"),
        from_user=SimpleNamespace(id=4321),
        message_id=40,
    )
    monkeypatch.setattr(
        private_music_playlist,
        "list_private_tracks_raw",
        AsyncMock(return_value=[]),
    )
    add_remote = AsyncMock(
        return_value={
            "id": 1,
            "service": "spotify",
            "source_url": "https://open.spotify.com/track/abc12345",
            "title": "Song",
            "performer": "Artist",
            "telegram_file_id": "spotify-file",
            "duration_seconds": 180,
            "source_message_id": 40,
            "audio_message_id": 41,
        }
    )
    monkeypatch.setattr(
        private_music_playlist,
        "add_remote_music_group_track",
        add_remote,
    )

    stored = await private_music_playlist.record_private_audio(
        message,
        service="spotify",
        source_url="https://open.spotify.com/track/abc12345",
        file_id="spotify-file",
        audio_message_id=41,
        title="Song",
        performer="Artist",
        duration=180,
    )

    assert stored is True
    add_remote.assert_awaited_once()


def test_message_has_audio_payload_detects_audio_voice_and_audio_document():
    assert private_music._message_has_audio_payload(
        SimpleNamespace(audio=object(), voice=None, document=None)
    )
    assert private_music._message_has_audio_payload(
        SimpleNamespace(audio=None, voice=object(), document=None)
    )
    assert private_music._message_has_audio_payload(
        SimpleNamespace(
            audio=None,
            voice=None,
            document=SimpleNamespace(mime_type="audio/mpeg"),
        )
    )
    assert not private_music._message_has_audio_payload(
        SimpleNamespace(
            audio=None,
            voice=None,
            document=SimpleNamespace(mime_type="video/mp4"),
        )
    )
