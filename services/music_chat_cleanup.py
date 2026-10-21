"""Safe text-only cleanup and manual duplicate-audio tombstones for MusiX.

Never infer message content from Telegram message-ID ranges. Telegram bots
cannot retrieve arbitrary chat history; only explicitly tracked text messages
may be removed by /clearall or the 24-hour worker.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, select

from services.logger import logger as logging
from services.storage.models import MusicChatCleanupEntry

logging = logging.bind(service="music_chat_cleanup")
_TEXT_KIND = "text"
_DELETED_AUDIO_KIND = "deleted_audio"


def _database():
    from app_context import db
    return db


def _bot():
    from app_context import bot
    return bot


def _chat_type(chat: Any) -> str | None:
    raw = str(getattr(chat, "type", "") or "").lower().split(".")[-1]
    if raw == "private":
        return "private"
    if raw in {"group", "supergroup"}:
        return "group"
    return None


async def _allowed_group(chat_id: int) -> bool:
    from services.admin_music_monitor import get_admin_music_monitor_group

    monitor_id, _title = await get_admin_music_monitor_group()
    if monitor_id is not None and int(chat_id) == int(monitor_id):
        return False
    database = _database()
    checker = getattr(database, "is_music_group_connected", None)
    if not callable(checker):
        return False
    return bool(await checker(int(chat_id)))


async def _save_entry(chat_id: int, message_id: int, chat_type: str, kind: str) -> None:
    database = _database()
    if not hasattr(database, "SessionLocal"):
        return
    async with database.SessionLocal() as session:
        async with session.begin():
            row = await session.get(
                MusicChatCleanupEntry,
                (int(chat_id), int(message_id)),
            )
            if row is None:
                session.add(MusicChatCleanupEntry(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    chat_type=chat_type,
                    kind=kind,
                ))


async def track_text_message(message: Any) -> None:
    """Record only ordinary text messages; captions and ALL media are excluded."""
    if not isinstance(getattr(message, "text", None), str) or not message.text:
        return
    if any(getattr(message, name, None) is not None for name in (
        "audio", "voice", "document", "video", "video_note", "photo",
        "animation", "sticker",
    )):
        return
    chat = getattr(message, "chat", None)
    chat_type = _chat_type(chat)
    if chat_type is None:
        return
    if getattr(message, "guest_query_id", None):
        return
    user = getattr(message, "from_user", None)
    if user is not None and getattr(user, "is_bot", False):
        return
    if chat_type == "group":
        try:
            if not await _allowed_group(int(chat.id)):
                return
        except Exception as exc:
            logging.debug("Group text tracking gate unavailable: %s", exc)
            return
    try:
        await _save_entry(int(chat.id), int(message.message_id), chat_type, _TEXT_KIND)
    except Exception as exc:
        logging.warning("Text tracking failed: chat=%s error=%s", chat.id, exc)


async def track_outbound_bot_text(make_request, bot, method):
    """Session middleware: catches bot replies/status messages, not audio captions."""
    from aiogram.methods import SendMessage

    result = await make_request(bot, method)
    if isinstance(method, SendMessage):
        chat = getattr(result, "chat", None)
        message_id = getattr(result, "message_id", None)
        chat_type = _chat_type(chat)
        if chat is not None and chat_type is not None and message_id is not None:
            try:
                await track_bot_text(int(chat.id), int(message_id), chat_type)
            except Exception as exc:
                logging.warning("Outbound text tracking failed: %s", exc)
    return result


async def track_bot_text(chat_id: int, message_id: int | None, chat_type: str) -> None:
    if message_id is None or chat_type not in {"private", "group"}:
        return
    try:
        if chat_type == "group" and not await _allowed_group(chat_id):
            return
        await _save_entry(chat_id, message_id, chat_type, _TEXT_KIND)
    except Exception as exc:
        logging.warning("Bot text tracking failed: chat=%s error=%s", chat_id, exc)


async def get_text_message_ids(chat_id: int) -> set[int]:
    database = _database()
    if not hasattr(database, "SessionLocal"):
        return set()
    async with database.SessionLocal() as session:
        values = await session.scalars(
            select(MusicChatCleanupEntry.message_id).where(
                MusicChatCleanupEntry.chat_id == int(chat_id),
                MusicChatCleanupEntry.kind == _TEXT_KIND,
            )
        )
        return {int(value) for value in values.all()}


async def remove_text_entries(chat_id: int, message_ids: Iterable[int]) -> None:
    ids = sorted({int(v) for v in message_ids})
    database = _database()
    if not ids or not hasattr(database, "SessionLocal"):
        return
    async with database.SessionLocal() as session:
        async with session.begin():
            await session.execute(
                delete(MusicChatCleanupEntry).where(
                    MusicChatCleanupEntry.chat_id == int(chat_id),
                    MusicChatCleanupEntry.kind == _TEXT_KIND,
                    MusicChatCleanupEntry.message_id.in_(ids),
                )
            )


async def mark_deleted_audio(chat_id: int, chat_type: str, message_ids: Iterable[int]) -> None:
    for message_id in sorted({int(v) for v in message_ids if int(v) > 0}):
        await _save_entry(chat_id, message_id, chat_type, _DELETED_AUDIO_KIND)


async def active_audio_tracks(chat_id: int, rows: Iterable[Any]) -> list[Any]:
    tracks = list(rows)
    if not tracks:
        return []
    database = _database()
    if not hasattr(database, "SessionLocal"):
        return tracks
    async with database.SessionLocal() as session:
        values = await session.scalars(
            select(MusicChatCleanupEntry.message_id).where(
                MusicChatCleanupEntry.chat_id == int(chat_id),
                MusicChatCleanupEntry.kind == _DELETED_AUDIO_KIND,
            )
        )
        removed = {int(value) for value in values.all()}
    return [
        track for track in tracks
        if getattr(track, "audio_message_id", None) is None
        or int(track.audio_message_id) not in removed
    ]


def duplicate_audio_message_ids(tracks: Iterable[Any]) -> list[int]:
    """Keep first (oldest Telegram message ID) per real song identity."""
    from services.music_group_dedupe import dedupe_music_group_tracks

    unique = {}
    for track in tracks:
        msg_id = getattr(track, "audio_message_id", None)
        if msg_id is not None:
            unique[int(msg_id)] = track
    _kept, duplicates = dedupe_music_group_tracks(
        sorted(unique.values(), key=lambda row: int(row.audio_message_id))
    )
    return [
        int(track.audio_message_id)
        for track in duplicates
        if getattr(track, "audio_message_id", None) is not None
    ]


async def remove_only_duplicate_audio(chat_id: int, chat_type: str, tracks: Iterable[Any]) -> tuple[int, int]:
    """Delete *only* identified repeated audios; not links, captions, or text."""
    filtered = await active_audio_tracks(chat_id, tracks)
    ids = duplicate_audio_message_ids(filtered)
    deleted, failed = await _delete_ids(_bot(), chat_id, ids)
    if deleted:
        await mark_deleted_audio(chat_id, chat_type, deleted)
    return len(deleted), len(failed)


async def _delete_ids(bot: Any, chat_id: int, ids: Iterable[int]) -> tuple[set[int], set[int]]:
    identifiers = sorted({int(v) for v in ids if int(v) > 0})
    deleted: set[int] = set()
    failed: set[int] = set()
    # Bulk deletion may report success when some messages were already gone.
    # Tombstones still ensure they aren't repeatedly selected for deletion.
    for start in range(0, len(identifiers), 100):
        batch = identifiers[start:start + 100]
        bulk = getattr(bot, "delete_messages", None)
        if callable(bulk):
            try:
                await bulk(chat_id=chat_id, message_ids=batch)
                deleted.update(batch)
                continue
            except Exception:
                pass
        for message_id in batch:
            try:
                await bot.delete_message(chat_id, message_id)
                deleted.add(message_id)
            except Exception:
                failed.add(message_id)
        await asyncio.sleep(0.025)
    return deleted, failed


async def _known_audio_message_ids(chat_id: int, chat_type: str) -> set[int]:
    """Second safety check for text entries that may have changed to audio."""
    if chat_type == "private":
        from services.private_music_playlist import list_private_tracks_raw
        tracks = await list_private_tracks_raw(chat_id)
    else:
        database = _database()
        getter = getattr(database, "list_music_group_tracks_raw", None)
        if not callable(getter):
            raise RuntimeError("Audio registry unavailable; skipping automatic cleanup")
        tracks = await getter(chat_id)
    return {
        int(row.audio_message_id)
        for row in tracks
        if getattr(row, "audio_message_id", None) is not None
    }


async def auto_clear_text_after_24h() -> None:
    """Hourly check: process text aged >=24h while Telegram's 48h limit allows.

    Only messages explicitly tracked as text; never captions or any audio.
    """
    database = _database()
    if not hasattr(database, "SessionLocal"):
        return
    now = datetime.now(timezone.utc)
    lower = now - timedelta(hours=24)
    # Telegram cannot delete messages older than 48h; leave a safety margin.
    upper = now - timedelta(hours=47)
    async with database.SessionLocal() as session:
        rows = (await session.execute(
            select(
                MusicChatCleanupEntry.chat_id,
                MusicChatCleanupEntry.message_id,
                MusicChatCleanupEntry.chat_type,
            )
            .where(
                MusicChatCleanupEntry.kind == _TEXT_KIND,
                MusicChatCleanupEntry.created_at <= lower,
                MusicChatCleanupEntry.created_at >= upper,
            )
            .order_by(MusicChatCleanupEntry.created_at.asc())
            .limit(400)
        )).all()
    by_chat: dict[tuple[int, str], list[int]] = defaultdict(list)
    for chat_id, message_id, chat_type in rows:
        by_chat[(int(chat_id), str(chat_type))].append(int(message_id))
    for (chat_id, chat_type), msg_ids in by_chat.items():
        try:
            if chat_type == "group" and not await _allowed_group(chat_id):
                continue
            if chat_type not in {"private", "group"}:
                continue
            known_audio = await _known_audio_message_ids(chat_id, chat_type)
            safe_text_ids = [value for value in msg_ids if value not in known_audio]
            if not safe_text_ids:
                continue
            deleted, failed = await _delete_ids(_bot(), chat_id, safe_text_ids)
            if deleted:
                await remove_text_entries(chat_id, deleted)
            logging.info(
                "24h text cleanup: chat=%s kind=%s deleted=%s failed=%s",
                chat_id, chat_type, len(deleted), len(failed),
            )
        except Exception as exc:
            logging.warning("24h text cleanup failed: chat=%s error=%s", chat_id, exc)
