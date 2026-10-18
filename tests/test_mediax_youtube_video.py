"""Offline parity tests for MediaX's unchanged YouTube source and Musix delivery."""
from __future__ import annotations

import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from services.media import mediax_youtube_video as bridge


EXPECTED_MEDIAX_HASHES = {
    "downloader.js": "605f89df5d6a49dbf2c031d1f16c2d0a7e62552173a6329e4b1c3bfa21ca0252",
    "youtube-free.js": "ea43ffb58e0ca5c0fdd10c6225cb3fff6ba3864ddfe72cb8b09d9b3e6217615d",
    "youtube-upload.js": "8a355ab13b5a14a7d94b219d421c7ce0610e865afb2884b166a7a9d5192cd574",
    "env-number.js": "85684489063536215b852f381c64a62803d4d9b66f46da2c47d161585432e9e4",
    "features/downloader.js": "0d798e45901317bea47d8daceea0d76dd96e673dde3b089e3cba6286ec4175f5",
    "bot/media-resolver.js": "5b4b4816d80edaee04d568aa497b710f5eab83d8cd1a618360a95958def0eb05",
    "telegram.js": "cf6addf7ae70a33a1fb182f2a622ea7f2523368080cd4ad82a71d3340e2d05c7",
}


@pytest.mark.parametrize("name,sha", EXPECTED_MEDIAX_HASHES.items())
def test_live_mediax_original_js_is_byte_identical(name, sha):
    code = (bridge._RUNTIME / "src" / name).read_bytes()
    assert hashlib.sha256(code).hexdigest() == sha


def test_node_imports_unchanged_mediax_modules():
    command = """
        import {parseMedia} from './src/downloader.js';
        import {parseYouTubeFree} from './src/youtube-free.js';
        import {prepareYouTubeTelegramUpload} from './src/youtube-upload.js';
        if (![parseMedia, parseYouTubeFree, prepareYouTubeTelegramUpload].every(x=>typeof x==='function')) process.exit(3);
        console.log('MEDIAX_ORIGINAL_MODULES_OK');
    """
    response = subprocess.run(
        [bridge._node_path(), "--input-type=module", "-e", command],
        cwd=bridge._RUNTIME,
        capture_output=True, text=True, timeout=20, check=True,
    )
    assert "MEDIAX_ORIGINAL_MODULES_OK" in response.stdout


def test_only_youtube_ids_accepted():
    assert bridge._video_id("https://youtu.be/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert bridge._video_id("https://youtube.com/watch?v=I1zqCX3Dvxo") == "I1zqCX3Dvxo"
    for url in ("http://youtu.be/jNQXAC9IVRw", "https://evil.example/watch?v=jNQXAC9IVRw"):
        with pytest.raises(bridge.MediaXVideoError):
            bridge._video_id(url)


def test_safe_url_does_not_allow_localhost_or_private_ip():
    assert bridge._safe_source_url("https://video.googlevideo.com/x")
    assert not bridge._safe_source_url("https://127.0.0.1/video")
    assert not bridge._safe_source_url("https://10.0.0.1/video")
    assert not bridge._safe_source_url("https://localhost/video")


class FakeBot:
    def __init__(self, *, url_success: bool = True):
        self.calls = []
        self.url_success = url_success

    async def send_video(self, **kwargs):
        self.calls.append(kwargs)
        if not self.url_success:
            raise RuntimeError("Telegram URL fetch failed")
        return SimpleNamespace(
            video=SimpleNamespace(file_id="real-file-id", file_size=12345),
            message_id=812,
        )


@pytest.mark.asyncio
async def test_mediax_url_fallback_uses_telegram_sendvideo(monkeypatch):
    monkeypatch.setattr(bridge, "_run_original_mediax", lambda _url: {
        "status": "candidates",
        "mediaxRevision": "08e1b510dfa7396d6b1d05561918db30c604f0c7",
        "candidates": [{"url": "https://videos.googlevideo.com/video.mp4", "quality": "360p"}],
    })
    bot = FakeBot()
    result = await bridge.send_mediax_youtube_video(
        bot, 123, "https://youtu.be/jNQXAC9IVRw"
    )
    assert result["strategy"] == "mediax-telegram-sendVideo-url"
    assert result["message_id"] == 812
    assert bot.calls[0]["chat_id"] == 123
    assert bot.calls[0]["video"].startswith("https://")


@pytest.mark.asyncio
async def test_video_local_upload_is_cleaned(monkeypatch, tmp_path):
    # Origin MediaX returns a real local video file. The adapter must verify,
    # upload and remove the tempfile, not emit a URL.
    video = tmp_path / ("ar-youtube-" + "1" * 32 + ".mp4")
    video.write_bytes(b"video" * 600)
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(bridge, "_run_original_mediax", lambda _url: {
        "status": "local_file", "filePath": str(video),
        "mediaxRevision": "08e1b510dfa7396d6b1d05561918db30c604f0c7",
    })
    monkeypatch.setattr(bridge, "_verify_video", lambda _path: None)
    bot = FakeBot()
    output = await bridge.send_mediax_youtube_video(bot, 123, "https://youtu.be/jNQXAC9IVRw")
    assert output["strategy"] == "mediax-local-video-upload"
    assert not video.exists()
