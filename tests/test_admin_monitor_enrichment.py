"""Monitor usage must be exact, persistent, and safe before PayPing cutover."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from services import admin_monitor_access as access
from services import admin_music_monitor as monitor


@pytest.mark.asyncio
async def test_private_success_count_is_idempotent_across_repeated_monitor_calls(monkeypatch):
    events = set()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        async def execute(self, stmt):
            values = stmt.compile().params
            events.add((int(values["user_id"]), int(values["request_message_id"])))

        async def scalar(self, stmt):
            return sum(uid == 42 for uid, _ in events)

    monkeypatch.setattr(monitor, "db", SimpleNamespace(SessionLocal=FakeSession))
    assert await monitor._record_private_audio_uses(42, 1001) == 1
    assert await monitor._record_private_audio_uses(42, 1001) == 1
    assert await monitor._record_private_audio_uses(42, 1002) == 2
    assert events == {(42, 1001), (42, 1002)}


def test_unconfigured_payping_does_not_invent_a_tier_or_policy_lock(monkeypatch):
    monkeypatch.delenv("PAYPING_BOT_API_KEY", raising=False)
    monkeypatch.delenv("PAYPING_API_BASE_URL", raising=False)
    lines = access.monitor_access_lines(access.MonitorAccess())
    assert "Belum disambung" in lines[0]
    assert "audio berjaya dihantar" in lines[1]
    assert "Locked" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_payping_readonly_check_after_connection(monkeypatch):
    monkeypatch.setenv("PAYPING_API_BASE_URL", "https://payping.example")
    monkeypatch.setenv("PAYPING_BOT_API_KEY", "secret-key")
    original_client = httpx.AsyncClient
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "allowed": False,
                "supporter": True,
                "supportTier": "Support Gold",
                "reason": "SUPPORT_REQUIRED",
                "mode": "force_support",
                "uses": 5,
            },
        )

    monkeypatch.setattr(
        access.httpx, "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            follow_redirects=kwargs["follow_redirects"],
        ),
    )
    result = await access.payping_monitor_status(
        12345, first_name="Listener", username="listener"
    )
    assert result.connected is True
    assert result.allowed is False
    assert result.tier == "Support Gold"
    assert result.payping_uses == 5
    lines = access.monitor_access_lines(result)
    assert "Support Gold" in lines[0]
    assert "Locked" in lines[1]
    assert "5 kali" in lines[2]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/api/bot-access/musix/check"
    assert request.headers["x-payping-bot-key"] == "secret-key"
    assert b'"notifyBlocked":false' in request.content
    assert b"consume" not in request.content


@pytest.mark.asyncio
async def test_payping_offline_does_not_lock_users(monkeypatch):
    monkeypatch.setenv("PAYPING_API_BASE_URL", "https://payping.example")
    monkeypatch.setenv("PAYPING_BOT_API_KEY", "secret-key")
    original_client = httpx.AsyncClient

    def handler(req):
        return httpx.Response(503, json={"ok": False, "message": "Service unavailable"})

    monkeypatch.setattr(
        access.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    status = await access.payping_monitor_status(12345)
    assert status.unavailable is True
    lines = access.monitor_access_lines(status)
    assert all("Tidak dapat disahkan" in line for line in lines)


@pytest.mark.asyncio
async def test_monitor_caption_includes_real_success_count_and_live_tier(monkeypatch):
    fake_bot = SimpleNamespace(send_audio=AsyncMock())
    monkeypatch.setattr(monitor, "bot", fake_bot)
    monkeypatch.setattr(
        monitor,
        "get_admin_music_monitor_group",
        AsyncMock(return_value=(-100100, "Monitor")),
    )
    count = AsyncMock(return_value=7)
    monkeypatch.setattr(monitor, "_record_private_audio_uses", count)
    check = AsyncMock(
        return_value=access.MonitorAccess(
            connected=True,
            allowed=True,
            supporter=True,
            tier="Support Plus",
            payping_uses=6,
        )
    )
    monkeypatch.setattr(monitor, "payping_monitor_status", check)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=12345, type="private"),
        from_user=SimpleNamespace(
            id=12345,
            first_name="Listener",
            full_name="Test Listener",
            username="listener",
        ),
        date=datetime(2026, 10, 18, tzinfo=timezone.utc),
        message_id=55,
    )
    await monitor.mirror_private_audio_to_admin_group(
        message, file_id="audio-file-id", platform="YouTube"
    )
    count.assert_awaited_once_with(12345, 55)
    check.assert_awaited_once()
    caption = fake_bot.send_audio.await_args.kwargs["caption"]
    assert "Penggunaan MusiX: 7 kali" in caption
    assert "Support Tier: Support Plus" in caption
    assert "Status Akses: ✅ Access" in caption
    assert "Penggunaan PayPing: 6 kali" in caption
