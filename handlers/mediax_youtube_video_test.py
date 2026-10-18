"""Owner-only YouTube VIDEO proof command; never added to Telegram menu."""
from __future__ import annotations

import logging

from aiogram import Router, types
from aiogram.filters import Command

from app_context import bot
from config import ADMINS_UID
from services.media.mediax_youtube_video import send_mediax_youtube_video

router = Router(name="mediax_youtube_video_test")
log = logging.getLogger(__name__)


@router.message(Command("testyoutubevideo"))
async def run_youtube_video_proof(message: types.Message) -> None:
    user = message.from_user
    if not user or int(user.id) not in ADMINS_UID or message.chat.type != "private":
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Contoh: /testyoutubevideo https://www.youtube.com/watch?v=jNQXAC9IVRw")
        return
    status = await message.answer("🎬 Sedang cuba MediaX original untuk VIDEO YouTube…")
    try:
        result = await send_mediax_youtube_video(bot, message.chat.id, parts[1].strip())
    except Exception as exc:
        log.warning("MediaX YouTube video test failure: type=%s", type(exc).__name__)
        await status.edit_text("❌ Video belum berjaya dihantar. Punca: " + str(exc)[-650:])
        return
    await status.edit_text(
        "✅ Video sebenar sudah dihantar!\n"
        f"Laluan MediaX: {result['strategy']}\n"
        f"Message ID: {result['message_id']}\n"
        f"Saiz: {result.get('size') or 'tidak diketahui'} bytes\n"
        "Ujian VIDEO sahaja; MP3 belum dibuat."
    )
