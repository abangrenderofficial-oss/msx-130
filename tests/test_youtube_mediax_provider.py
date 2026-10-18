"""Offline proof: Musix receives actual video bytes via MediaX-like providers."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from services.media import youtube_mediax_provider as provider
from services.media import youtube_video_audio_fallback as fallback


def test_youtube_provider_accepts_real_youtube_ids_only():
    assert provider._youtube_id("https://youtu.be/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert provider._youtube_id(
        "https://www.youtube.com/watch?v=I1zqCX3Dvxo"
    ) == "I1zqCX3Dvxo"
    with pytest.raises(ValueError):
        provider._youtube_id("https://example.org/watch?v=jNQXAC9IVRw")


def test_provider_blocks_private_stream_targets():
    assert provider._stream_url_ok("https://video.example.com/stream.mp4")
    assert not provider._stream_url_ok("http://127.0.0.1/stream.mp4")
    assert not provider._stream_url_ok("https://10.0.0.1/stream.mp4")
    assert not provider._stream_url_ok("https://localhost/stream.mp4")


def test_weirddl_strategy_downloads_actual_video_bytes(monkeypatch, tmp_path):
    original = httpx.Client
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"title": "test clip"})
        if request.url.path == "/api/download":
            return httpx.Response(200, content=b"video" * 650,
                                  headers={"content-type": "video/mp4"})
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        provider.httpx, "Client",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    file, source = provider.download_video_from_mediax_providers(
        "https://youtu.be/jNQXAC9IVRw",
        str(tmp_path / "video.%(ext)s"), 8 * 1024 * 1024,
    )
    assert source == "weirddl-stream"
    assert Path(file).read_bytes() == b"video" * 650
    assert len(urls) == 2


def test_provider_refuses_html_page_even_if_status_200(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, text="<html>error</html>",
            headers={"content-type": "text/html"},
        )
    ))
    with client:
        with pytest.raises(ValueError, match="error page"):
            provider._receive(
                client, "https://video.example.com/video.mp4",
                str(tmp_path / "video.mp4"), 5 * 1024 * 1024,
            )


def test_provider_rescue_downloads_video_before_ffmpeg(monkeypatch, tmp_path):
    called = []

    class BlockedDL:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def extract_info(self, *_args, **_kwargs):
            raise RuntimeError("YouTube blocked server")
        def download(self, *_args, **_kwargs):
            called.append("native_download_blocked")
            raise RuntimeError("YouTube blocked server")

    def fake_provider(_url, out_template, _limit):
        called.append("provider_video_download")
        p = out_template.replace(".%(ext)s", ".mp4")
        Path(p).write_bytes(b"video" * 650)
        return p, "weirddl-stream"

    def fake_ffmpeg(cmd, **_kwargs):
        called.append(cmd[0])
        if cmd[0] == "ffmpeg":
            assert "provider_video_download" in called
            Path(cmd[-1]).write_bytes(b"audio" * 650)
            return SimpleNamespace(stdout="")
        if cmd[0] == "ffprobe":
            types = ([{"codec_name": "mp3", "codec_type": "audio"}]
                     if cmd[-1].endswith(".mp3")
                     else [{"codec_name": "aac", "codec_type": "audio"},
                           {"codec_name": "h264", "codec_type": "video"}])
            return SimpleNamespace(stdout=json.dumps({
                "streams": types, "format": {"duration": "5"}
            }))
        raise AssertionError("unknown subprocess")

    monkeypatch.setattr(fallback, "YoutubeDL", BlockedDL)
    monkeypatch.setattr(provider, "download_video_from_mediax_providers", fake_provider)
    monkeypatch.setattr(fallback.subprocess, "run", fake_ffmpeg)

    output = fallback.download_youtube_video_as_mp3(
        "https://youtu.be/jNQXAC9IVRw",
        str(tmp_path / "music.%(ext)s"),
        bitrate_kbps=192,
    )
    assert Path(output).exists()
    assert called.index("provider_video_download") < called.index("ffmpeg")
    assert not list(tmp_path.glob("music-video.*"))
