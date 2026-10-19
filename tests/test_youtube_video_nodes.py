"""Worker router and cookie-aware failover tests for Musix V2."""
from __future__ import annotations

import json
import subprocess

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.media.youtube_video_nodes import (
    WorkerNode, WorkerRouterError, configured_nodes, download_from_worker_nodes,
)
from services.media.youtube_video_worker_api import canonical_youtube_url, make_app


def _mp3(tmp_path) -> bytes:
    path = tmp_path / "sample.mp3"
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return path.read_bytes()


def _nodes() -> list[WorkerNode]:
    return [
        WorkerNode(name="cookie_node", base_url="https://cookie.example", token="s" * 32),
        WorkerNode(name="guest_node", base_url="https://guest.example", token="s" * 32),
    ]


def test_nodes_require_https_and_secrets_in_environment(monkeypatch):
    monkeypatch.setenv("MUSIX_VIDEO_FIRST_NODES_JSON", json.dumps([
        {"name": "a", "url": "https://node.example", "token_env": "NODE_A_TOKEN"},
    ]))
    monkeypatch.setenv("NODE_A_TOKEN", "s" * 32)
    nodes = configured_nodes()
    assert len(nodes) == 1 and nodes[0].name == "a"
    assert nodes[0].token == "s" * 32

    monkeypatch.setenv("MUSIX_VIDEO_FIRST_NODES_JSON", json.dumps([
        {"name": "a", "url": "http://node.example", "token_env": "NODE_A_TOKEN"},
    ]))
    with pytest.raises(WorkerRouterError, match="HTTPS"):
        configured_nodes()

    monkeypatch.setenv("MUSIX_VIDEO_FIRST_NODES_JSON", json.dumps([
        {"name": "a", "url": "https://node.example", "token_env": "MISSING_SECRET"},
    ]))
    with pytest.raises(WorkerRouterError, match="secret"):
        configured_nodes()


@pytest.mark.asyncio
async def test_cookie_node_auth_challenge_fails_over_to_guest_node(tmp_path):
    calls: list[str] = []
    payload = _mp3(tmp_path)

    def request(request: httpx.Request):
        calls.append(str(request.url))
        if request.method == "GET" and request.url.host == "cookie.example":
            return httpx.Response(200, json={
                "ok": True, "capacity": 2, "active": 0,
                "cookie_domains": ["youtube.com"],
            })
        if request.method == "GET":
            return httpx.Response(200, json={
                "ok": True, "capacity": 2, "active": 0, "cookie_domains": [],
            })
        if request.url.host == "cookie.example":
            return httpx.Response(409, json={"error": "context_unavailable"})
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        output = await download_from_worker_nodes(
            "https://youtu.be/dQw4w9WgXcQ",
            str(tmp_path / "source.%(ext)s"),
            128, nodes=_nodes(), client=client,
        )
    assert output.endswith("source.mp3")
    assert (tmp_path / "source.mp3").read_bytes() == payload
    assert calls.index("https://cookie.example/v1/audio") < calls.index(
        "https://guest.example/v1/audio"
    )


@pytest.mark.asyncio
async def test_worker_auth_error_not_silently_retried(tmp_path):
    calls = []

    def request(request):
        calls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json={
                "ok": True, "capacity": 1, "active": 0,
                "cookie_domains": ["youtube.com"] if request.url.host == "cookie.example" else [],
            })
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        with pytest.raises(WorkerRouterError, match="worker_auth"):
            await download_from_worker_nodes(
                "https://youtu.be/dQw4w9WgXcQ",
                str(tmp_path / "source.%(ext)s"),
                128, nodes=_nodes(), client=client,
            )
    assert len([url for url in calls if url.endswith("/v1/audio")]) == 1
    assert not (tmp_path / "source.mp3").exists()


@pytest.mark.asyncio
async def test_invalid_media_cannot_be_sent_to_telegram(tmp_path):
    def request(request):
        if request.method == "GET":
            return httpx.Response(200, json={
                "ok": True, "capacity": 1, "active": 0, "cookie_domains": [],
            })
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=b"<html>bad media</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        with pytest.raises(WorkerRouterError, match="all_worker_nodes_failed"):
            await download_from_worker_nodes(
                "https://youtu.be/dQw4w9WgXcQ",
                str(tmp_path / "source.%(ext)s"),
                128, nodes=_nodes(), client=client,
            )
    assert not (tmp_path / "source.mp3").exists()


def test_worker_only_accepts_youtube_https_urls():
    good = canonical_youtube_url("https://youtu.be/dQw4w9WgXcQ?t=3")
    assert good == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    for invalid in (
        "http://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.attacker.invalid/watch?v=dQw4w9WgXcQ",
        "https://127.0.0.1/private",
        "file:///tmp/secret",
        "https://www.youtube.com/redirect?url=https://example.com",
    ):
        with pytest.raises(ValueError):
            canonical_youtube_url(invalid)


@pytest.mark.asyncio
async def test_worker_requires_shared_secret(tmp_path):
    async def fake_extractor(url, out_template, bitrate):
        output = out_template.replace(".%(ext)s", ".mp3")
        with open(output, "wb") as target:
            target.write(b"x" * 2048)
        return output

    app = make_app(secret="k" * 32, extractor=fake_extractor)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            unauthorized = await client.get("/v1/health")
            assert unauthorized.status == 401
            authorized = await client.get(
                "/v1/health", headers={"X-Musix-Worker-Token": "k" * 32},
            )
            assert authorized.status == 200
            assert (await authorized.json())["cookie_domains"] == []
            bad = await client.post(
                "/v1/audio", json={"url": "https://example.com", "bitrate_kbps": 128},
                headers={"X-Musix-Worker-Token": "k" * 32},
            )
            assert bad.status == 400


@pytest.mark.asyncio
async def test_worker_streams_mp3_and_cleans_temporary_files(tmp_path):
    payload = _mp3(tmp_path)

    async def fake_extractor(url, out_template, bitrate):
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert bitrate == 192
        path = out_template.replace(".%(ext)s", ".mp3")
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    app = make_app(secret="t" * 32, extractor=fake_extractor)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            answer = await client.post(
                "/v1/audio",
                json={"url": "https://youtu.be/dQw4w9WgXcQ", "bitrate_kbps": 192},
                headers={"X-Musix-Worker-Token": "t" * 32},
            )
            assert answer.status == 200
            assert answer.headers.get("Content-Type") == "audio/mpeg"
            assert (await answer.read()) == payload


@pytest.mark.asyncio
async def test_musix_opt_in_routes_to_worker_nodes(monkeypatch, tmp_path):
    from services.media import music_download, youtube_video_nodes
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED", "1")
    monkeypatch.setattr(
        youtube_video_nodes, "configured_nodes",
        lambda: _nodes(),
    )
    async def fake_route(url, template, bitrate, *, nodes):
        assert nodes == _nodes()
        return str(tmp_path / "source.mp3")
    monkeypatch.setattr(youtube_video_nodes, "download_from_worker_nodes", fake_route)
    output = await music_download._download_mp3(
        "https://youtu.be/dQw4w9WgXcQ", source="youtube",
        work_dir=str(tmp_path), bitrate_kbps=192,
    )
    assert output.endswith("source.mp3")


@pytest.mark.asyncio
async def test_corrupt_mp3_large_enough_for_ffprobe_still_fails_over(tmp_path):
    correct_audio = _mp3(tmp_path)
    calls = []

    def request(req):
        if req.method == "GET":
            return httpx.Response(200, json={
                "ok": True, "capacity": 2, "active": 0,
                "cookie_domains": ["youtube.com"] if req.url.host == "cookie.example" else [],
            })
        calls.append(req.url.host)
        if req.url.host == "cookie.example":
            return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=b"X" * 2048)
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=correct_audio)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        output = await download_from_worker_nodes(
            "https://youtu.be/dQw4w9WgXcQ",
            str(tmp_path / "source.%(ext)s"), 128,
            nodes=_nodes(), client=client,
        )
    assert calls == ["cookie.example", "guest.example"]
    assert (tmp_path / "source.mp3").read_bytes() == correct_audio
    assert output.endswith(".mp3")


@pytest.mark.asyncio
async def test_health_reports_worker_cookie_file_without_leaking_it(monkeypatch, tmp_path):
    source = tmp_path / "youtube.txt"
    source.write_text("# Netscape HTTP Cookie File\\n.youtube.com\\tTRUE\\t/\\tTRUE\\t0\\tTEST\\tsecret\\n")
    monkeypatch.setenv("YTDLP_YOUTUBE_COOKIES_FILE", str(source))
    app = make_app(secret="k" * 32)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.get(
                "/v1/health", headers={"X-Musix-Worker-Token": "k" * 32},
            )
            assert response.status == 200
            data = await response.json()
            assert data["cookie_domains"] == ["youtube.com"]
            assert str(source) not in str(data)
            assert "secret" not in str(data)
