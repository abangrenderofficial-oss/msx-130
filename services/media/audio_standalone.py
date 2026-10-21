"""Always send MusiX audio as a standalone Telegram message.

A handler may use Message.reply_audio for convenience (and to retain its
business/thread context), but the visible Telegram reply attachment must
not reference a source link that /clearall will later remove.

Only SendAudio requests are changed. Text, status replies, original caption,
audio metadata, music-group playlist IDs and admin-monitor forwards remain
untouched.
"""

from __future__ import annotations

from aiogram.methods import SendAudio


async def send_audio_standalone(make_request, bot, method):
    """Remove the Telegram quote/reply link while preserving every other field."""
    if isinstance(method, SendAudio) and (
        method.reply_parameters is not None
        or method.reply_to_message_id is not None
    ):
        method = method.model_copy(
            update={
                "reply_parameters": None,
                "reply_to_message_id": None,
            }
        )
    return await make_request(bot, method)
