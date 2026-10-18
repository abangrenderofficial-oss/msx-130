"""Offline regression tests for Musix video->audio fallback. No YouTube calls."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.media.music_download as music
import services.media.youtube_video_audio_fallback as fallback


def _format(format_id: str, height: int, abr: int | None, size: int = 4000):
    return {
        "format_id": format_id, "height": height, "abr": abr,
        "filesize": size, "acodec": "mp4a.40.2", "vcodec": "avc1.42E01E",
    }


def test_same_audio_quality_selects_smallest_video():
    items = [_format("1080", 1080, 192, 100000), _format("144", 144, 192, 10000)]
    assert fallback.select_video_audio_formats(items)[0]["format_id"] == "144"


def test_higher_audio_quality_wins_despite_larger_video():
    items = [_format("144", 144, 48, 2000), _format("720", 720, 192, 40000)]
    assert fallback.select_video_audio_formats(items)[0]["format_id"] == "720"


def test_excludes_video_only_audio_only_drm_and_oversize():
    items = [
        {**_format("videoonly", 144, 160), "acodec": "none"},
        {**_format("audioonly", 0, 160), "vcodec": "none"},
        {**_format("drm", 480, 256), "has_drm": True},
        _format("huge", 720, 256, 200_000),
        _format("good", 360, 128, 4_000),
    ]
    assert [f["format_id"] for f in fallback.select_video_audio_formats(items, max_bytes=100_000)] == ["good"]


def test_unknown_audio_bitrate_is_not_faked():
    items = [_format("unknown", 144, None, 100), _format("known", 360, 128, 500)]
    assert fallback.select_video_audio_formats(items)[0]["format_id"] == "known"
    assert fallback._mp3_bitrate_kbps(48, 320) == 48
    assert fallback._mp3_bitrate_kbps(128, 320) == 128
    assert fallback._mp3_bitrate_kbps(None, 320) == 160


def test_download_uses_smallest_muxed_format_and_validates_audio(monkeypatch, tmp_path):
    calls = []

    class FakeDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def extract_info(self, _url, download=False):
            assert download is False
            return {"formats": [_format("1080", 1080, 192, 10000), _format("144", 144, 192, 1000)]}
        def download(self, _urls):
            calls.append(self.opts)
            filename = self.opts["outtmpl"].replace(".%(ext)s", ".mp3")
            Path(filename).write_bytes(b"x" * 3000)
            return 0

    monkeypatch.setattr(fallback, "YoutubeDL", FakeDL)
    monkeypatch.setattr(fallback.subprocess, "run", lambda *_args, **_kw: SimpleNamespace(
        stdout=json.dumps({"streams": [{"codec_name": "mp3"}], "format": {"duration": "3"}})
    ))
    result = fallback.download_youtube_video_as_mp3(
        "https://youtu.be/demo", str(tmp_path / "source.%(ext)s"), bitrate_kbps=320
    )
    assert result.endswith("source.mp3")
    assert len(calls) == 1
    assert calls[0]["format"] == "144"
    assert calls[0]["postprocessors"][0]["preferredquality"] == "192"


def test_if_muxed_unavailable_tries_mediax_merge(monkeypatch, tmp_path):
    calls = []

    class FakeDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def extract_info(self, _url, download=False):
            return {"formats": [{**_format("video", 240, None), "acodec": "none"}]}
        def download(self, _urls):
            calls.append(self.opts["format"])
            Path(self.opts["outtmpl"].replace(".%(ext)s", ".mp3")).write_bytes(b"x" * 3000)
            return 0

    monkeypatch.setattr(fallback, "YoutubeDL", FakeDL)
    monkeypatch.setattr(fallback.subprocess, "run", lambda *_args, **_kw: SimpleNamespace(
        stdout=json.dumps({"streams": [{"codec_name": "mp3"}], "format": {"duration": "3"}})
    ))
    fallback.download_youtube_video_as_mp3("https://youtu.be/demo", str(tmp_path / "source.%(ext)s"), bitrate_kbps=256)
    assert len(calls) == 1 and "bestvideo[height<=480]+bestaudio" in calls[0]


def test_corrupt_mp3_rejected(tmp_path):
    path = tmp_path / "source.mp3"
    path.write_bytes(b"not audio" * 900)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError, ValueError)):
        fallback._verified_mp3(str(tmp_path / "source.%(ext)s"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_real_synthetic_mp3_validation(tmp_path):
    path = tmp_path / "source.mp3"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:a", "libmp3lame", "-b:a", "128k", str(path),
    ], check=True, timeout=15)
    assert fallback._verified_mp3(str(tmp_path / "source.%(ext)s")) == str(path)


@pytest.mark.asyncio
async def test_local_audio_failure_uses_video_fallback_before_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_YOUTUBE_LOCAL_FIRST", "1")
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_AUDIO_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(music, "_run_ytdlp_mp3_sync", lambda *_args: (_ for _ in ()).throw(RuntimeError("audio unavailable")))
    monkeypatch.setattr(fallback, "download_youtube_video_as_mp3", lambda *_args, **_kw: "/tmp/video-extracted.mp3")
    monkeypatch.setattr(music, "_youtube_worker_configured", lambda: (_ for _ in ()).throw(AssertionError("worker must not run")))
    result = await music._download_mp3("https://youtu.be/demo", work_dir=str(tmp_path), bitrate_kbps=192, source="youtube")
    assert result == "/tmp/video-extracted.mp3"


@pytest.mark.asyncio
async def test_failed_video_fallback_keeps_existing_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_YOUTUBE_LOCAL_FIRST", "1")
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_AUDIO_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(music, "_run_ytdlp_mp3_sync", lambda *_args: (_ for _ in ()).throw(RuntimeError("audio unavailable")))
    monkeypatch.setattr(fallback, "download_youtube_video_as_mp3", lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("no muxed streams")))
    monkeypatch.setattr(music, "_youtube_worker_configured", lambda: True)
    monkeypatch.setattr(music, "_run_youtube_worker_mp3_sync", lambda *_args: "/tmp/existing-worker.mp3")
    result = await music._download_mp3("https://youtu.be/demo", work_dir=str(tmp_path), bitrate_kbps=192, source="youtube")
    assert result == "/tmp/existing-worker.mp3"


@pytest.mark.asyncio
async def test_video_fallback_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_YOUTUBE_LOCAL_FIRST", "1")
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_AUDIO_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(music, "_run_ytdlp_mp3_sync", lambda *_args: (_ for _ in ()).throw(RuntimeError("audio unavailable")))
    monkeypatch.setattr(fallback, "download_youtube_video_as_mp3", lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("disabled")))
    monkeypatch.setattr(music, "_youtube_worker_configured", lambda: True)
    monkeypatch.setattr(music, "_run_youtube_worker_mp3_sync", lambda *_args: "/tmp/existing-worker.mp3")
    result = await music._download_mp3("https://youtu.be/demo", work_dir=str(tmp_path), bitrate_kbps=192, source="youtube")
    assert result == "/tmp/existing-worker.mp3"


@pytest.mark.asyncio
async def test_blocked_youtube_metadata_uses_title_only_to_reach_rescue(monkeypatch):
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_AUDIO_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        music, "_extract_info_sync",
        lambda *_a: (_ for _ in ()).throw(music.MusicDownloadError("YouTube login challenge")),
    )
    monkeypatch.setattr(
        music, "_youtube_oembed_info_sync",
        lambda _url: {"id": "abcdefghijk", "title": "Public track", "uploader": "Creator"},
    )
    result = await music.fetch_music_metadata(
        "https://youtu.be/abcdefghijk", source="youtube", timeout_seconds=5
    )
    assert result.title == "Public track"
    assert result.performer == "Creator"
    assert result.duration is None  # oEmbed is metadata, not verified audio


@pytest.mark.asyncio
async def test_metadata_rescue_disabled_preserves_original_error(monkeypatch):
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_AUDIO_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(
        music, "_extract_info_sync",
        lambda *_a: (_ for _ in ()).throw(music.MusicDownloadError("YouTube challenge")),
    )
    monkeypatch.setattr(
        music, "_youtube_oembed_info_sync",
        lambda *_url: (_ for _ in ()).throw(AssertionError("must not call oembed")),
    )
    with pytest.raises(music.MusicDownloadError, match="YouTube challenge"):
        await music.fetch_music_metadata(
            "https://youtu.be/abcdefghijk", source="youtube", timeout_seconds=5
        )
