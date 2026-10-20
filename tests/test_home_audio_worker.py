"""Standalone opt-in home-worker safety tests; no network or Telegram sends."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services import home_audio_queue as q
from services.home_audio_api import install_routes
from services.storage.models import Base


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'home_worker_test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = SimpleNamespace(
        SessionLocal=async_sessionmaker(engine, expire_on_commit=False),
        add_file=AsyncMock(),
    )
    yield database
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_flag_leaves_original_path_unchanged(monkeypatch):
    monkeypatch.delenv("HOME_WORKER_ENABLED", raising=False)
    monkeypatch.delenv("HOME_WORKER_API_TOKEN", raising=False)
    assert not q.enabled()
    assert not q.configured()
    app = web.Application()
    install_routes(app, object())
    assert not any("/api/home-worker/" in str(route.resource) for route in app.router.routes())


@pytest.mark.asyncio
async def test_heartbeat_ready_status_and_claim_fenced(db, monkeypatch):
    monkeypatch.setenv("HOME_WORKER_TIMEOUT_SECONDS", "90")
    assert not await q.is_online(db)
    await q.record_heartbeat(db, "windows-home", ready=True)
    assert await q.is_online(db)
    jid = await q.enqueue(
        db, video_id="I1zqCX3Dvxo", url="https://www.youtube.com/watch?v=I1zqCX3Dvxo",
        chat_id=-100123, message_id=48,
    )
    first = await q.claim(db, "windows-home")
    assert first["job_id"] == jid
    assert await q.claim(db, "another-worker") is None
    assert await q.complete(
        db, worker_id="wrong-worker", job_id=jid,
        claim_token=first["claim_token"], success=True, file_id="file1",
    ) is None
    assert await q.complete(
        db, worker_id="windows-home", job_id=jid,
        claim_token="wrong", success=True, file_id="file1",
    ) is None
    assert await q.complete(
        db, worker_id="windows-home", job_id=jid,
        claim_token=first["claim_token"], success=True,
        file_id="file1", title="Song", performer="Artist", duration=130,
    )
    result = await q.wait_for_result(db, jid)
    assert result["file_id"] == "file1"
    assert result["title"] == "Song"
    assert not await q.complete(
        db, worker_id="windows-home", job_id=jid,
        claim_token=first["claim_token"], success=True, file_id="duplicate",
    )


@pytest.mark.asyncio
async def test_failed_job_falls_back_without_retry_loop(db):
    await q.record_heartbeat(db, "home", ready=True)
    jid = await q.enqueue(
        db, video_id="I1zqCX3Dvxo", url="https://youtube.com/watch?v=I1zqCX3Dvxo",
        chat_id=123, message_id=1,
    )
    first = await q.claim(db, "home")
    report = await q.complete(
        db, worker_id="home", job_id=jid, claim_token=first["claim_token"],
        success=False, failure_reason="bot_check",
    )
    assert report["status"] == "failed"
    assert await q.wait_for_result(db, jid) is None
    assert await q.claim(db, "home") is None
    await q.record_heartbeat(db, "home", ready=False)
    assert not await q.is_online(db)
    data = await q.stats(db)
    assert data["24h"]["total"] == 1
    assert data["24h"]["success"] == 0
    assert data["7d"]["rate"] == 0.0


@pytest.mark.asyncio
async def test_expire_rejects_late_worker_and_records_timeout(db):
    jid = await q.enqueue(
        db, video_id="I1zqCX3Dvxo", url="https://youtu.be/I1zqCX3Dvxo",
        chat_id=123, message_id=12,
    )
    first = await q.claim(db, "home")
    await q.expire(db, jid)
    assert (await q.result(db, jid))["status"] == "timeout"
    assert await q.complete(
        db, worker_id="home", job_id=jid,
        claim_token=first["claim_token"], success=True, file_id="late-file"
    ) is None


@pytest.mark.asyncio
async def test_authenticated_pull_api_and_worker_cache(db, monkeypatch):
    monkeypatch.setenv("HOME_WORKER_ENABLED", "true")
    monkeypatch.setenv("HOME_WORKER_API_TOKEN", "q" * 48)
    from services.media import music_download
    from services.storage import music_cache
    cache = AsyncMock()
    monkeypatch.setattr(music_cache, "store_cached_audio", cache)
    app = web.Application()
    install_routes(app, db)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/home-worker/heartbeat", json={"worker_id": "home", "ready": True})
        assert response.status == 401
        header = {"Authorization": "Bearer " + "q" * 48}
        response = await client.post(
            "/api/home-worker/heartbeat", headers=header,
            json={"worker_id": "home", "ready": True},
        )
        assert response.status == 200
        jid = await q.enqueue(
            db, video_id="I1zqCX3Dvxo", url="https://www.youtube.com/watch?v=I1zqCX3Dvxo",
            chat_id=123, message_id=18,
        )
        response = await client.post("/api/home-worker/claim", headers=header, json={"worker_id": "home"})
        assert response.status == 200
        data = (await response.json())["job"]
        assert data["job_id"] == jid
        response = await client.post(
            "/api/home-worker/complete", headers=header,
            json={"worker_id": "home", "job_id": jid, "claim_token": data["claim_token"],
                  "success": True, "file_id": "test-file", "title": "Some Song",
                  "performer": "Artist", "duration": 144},
        )
        assert response.status == 200
        db.add_file.assert_awaited_once()
        cache.assert_awaited_once()
        assert (await q.result(db, jid))["file_id"] == "test-file"


def test_home_worker_uses_audio_only_and_disables_cookies_by_default(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / "home_worker" / "worker.py"
    spec = importlib.util.spec_from_file_location("test_worker_module", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.classify(Exception("Sign in to confirm you're not a bot")) == "bot_check"
    assert module.classify(Exception("HTTP Error 403")) == "http_403"
    monkeypatch.delenv("COOKIES_PATH", raising=False)
    monkeypatch.setenv("HOME_ENABLE_DENO", "false")
    captured = {}
    class YDL:
        def __init__(self, opts):
            captured.update(opts)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def extract_info(self, url, download=True):
            (tmp_path / "audio.mp3").write_bytes(b"\x00" * 1024)
            return {"title": "Test", "duration": 5}
    monkeypatch.setattr(module, "YoutubeDL", YDL)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="5.0"))
    module.extract_audio("https://www.youtube.com/watch?v=I1zqCX3Dvxo", tmp_path)
    assert captured["format"] == "bestaudio/best"
    assert "cookiefile" not in captured
    assert captured["noplaylist"] is True
