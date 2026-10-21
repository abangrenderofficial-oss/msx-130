"""MusiX MP3 must never reply to a source link deleted by /clearall."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendAudio, SendMessage
from aiogram.types import ReplyParameters

from main import create_app
from services.media.audio_standalone import send_audio_standalone


@pytest.mark.asyncio
async def test_reply_audio_becomes_standalone_and_keeps_its_metadata():
    method = SendAudio(
        chat_id=42,
        audio="telegram-file-id",
        title="My Music",
        caption="🎵 My Music\n@abangrender_musicdownloaderbot",
        performer="Singer",
        duration=225,
        thumbnail=None,
        reply_parameters=ReplyParameters(message_id=16),
        message_thread_id=300,
        business_connection_id="business-ctx",
    )
    sent = SimpleNamespace(message_id=40)
    make_request = AsyncMock(return_value=sent)
    result = await send_audio_standalone(make_request, object(), method)
    actual = make_request.await_args.args[1]

    assert result is sent
    assert isinstance(actual, SendAudio)
    assert actual.reply_parameters is None
    assert actual.reply_to_message_id is None
    assert actual.chat_id == 42
    assert actual.message_thread_id == 300
    assert actual.business_connection_id == "business-ctx"
    assert actual.audio == "telegram-file-id"
    assert actual.title == method.title
    assert actual.caption == method.caption
    assert actual.duration == method.duration
    assert actual.performer == method.performer
    # Do not mutate original request object passed by the music handler.
    assert method.reply_parameters.message_id == 16


@pytest.mark.asyncio
async def test_deprecated_reply_to_message_id_is_stripped():
    request = AsyncMock()
    await send_audio_standalone(
        request, object(), SendAudio(chat_id=-100123, audio="audio", reply_to_message_id=55),
    )
    assert request.await_args.args[1].reply_to_message_id is None


@pytest.mark.asyncio
async def test_plain_audio_does_not_gain_reply():
    method = SendAudio(chat_id=-100123, audio="audio", caption="Music")
    request = AsyncMock()
    await send_audio_standalone(request, object(), method)
    assert request.await_args.args[1] is method
    assert request.await_args.args[1].reply_parameters is None


@pytest.mark.asyncio
async def test_text_reply_remains_unchanged_for_status_messages():
    method = SendMessage(
        chat_id=42,
        text="Sedang memproses...",
        reply_parameters=ReplyParameters(message_id=16),
    )
    request = AsyncMock()
    await send_audio_standalone(request, object(), method)
    assert request.await_args.args[1] is method
    assert method.reply_parameters.message_id == 16


def test_real_musix_bot_registers_standalone_audio_middleware():
    # No Telegram networking needed to construct an application.
    app = create_app(
        bot_token="123456:unit-test-key",
        database_factory=lambda: object(),
    )
    from services.media.audio_standalone import send_audio_standalone

    assert send_audio_standalone in app.session.middleware


@pytest.mark.asyncio
async def test_actual_aiogram_bot_pipeline_strips_reply_before_telegram_transport():
    app = create_app(
        bot_token="123456:unit-test-key",
        database_factory=lambda: object(),
    )
    underlying_send = AsyncMock(return_value=SimpleNamespace(
        message_id=105,
        chat=SimpleNamespace(id=-10042, type="supergroup"),
    ))
    app.session.make_request = underlying_send
    result = await app.bot(
        SendAudio(
            chat_id=-10042,
            audio="file-id",
            caption="🎵 Song\n@abangrender_musicdownloaderbot",
            message_thread_id=77,
            reply_parameters=ReplyParameters(message_id=99),
        )
    )
    sent_method = underlying_send.await_args.args[1]
    assert result.message_id == 105
    assert sent_method.reply_parameters is None
    assert sent_method.reply_to_message_id is None
    assert sent_method.message_thread_id == 77
    assert sent_method.caption.endswith("@abangrender_musicdownloaderbot")
