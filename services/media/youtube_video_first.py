"""Video-first YouTube acquisition for MusiX (staging, opt-in).

Architecture informed by publicly documented ytdl_tg_bot worker design. This is
an independent Python implementation, not a copy of the Rust project.
Never uploads the downloaded video to Telegram. Requires an audio-bearing
video file before extracting MP3.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from yt_dlp import YoutubeDL

from services.platforms.youtube_media import build_ytdlp_youtube_options


class VideoFirstError(RuntimeError):
    """YouTube video acquisition or audio validation failed."""


def _media_formats(info: dict[str, Any], *, max_height: int) -> str:
    """Select a small audio-bearing video, or separate streams to merge."""
    entries = info.get("formats") or []
    video = [f for f in entries if f.get("vcodec") not in (None, "none") and f.get("format_id")]
    audio = [f for f in entries if f.get("acodec") not in (None, "none") and f.get("format_id")]
    combined = [f for f in video if f.get("acodec") not in (None, "none")]
    if not video or not audio:
        raise VideoFirstError("YouTube provided no downloadable video with an audio track")

    def under_limit(items: list[dict]) -> list[dict]:
        suitable = [f for f in items if int(f.get("height") or 0) <= max_height]
        return suitable or items

    def size(f: dict) -> int:
        return int(f.get("filesize") or f.get("filesize_approx") or 10**12)

    if combined:
        options = under_limit(combined)
        # Prefer highest known *audio* bitrate, breaking ties by smaller video.
        chosen = sorted(
            options,
            key=lambda f: (
                -float(f.get("abr") or 0),
                size(f),
                int(f.get("height") or 10**5),
            ),
        )[0]
        return str(chosen["format_id"])

    # Some YouTube videos have only separate streams. Download the smallest
    # practical video plus the best audio; yt-dlp/FFmpeg merge them locally.
    video_only = [f for f in video if f.get("acodec") in (None, "none")]
    audio_only = [f for f in audio if f.get("vcodec") in (None, "none")]
    if not video_only or not audio_only:
        raise VideoFirstError("Could not construct an audio-bearing video format")
    selected_video = min(
        under_limit(video_only),
        key=lambda f: (int(f.get("height") or 10**5), size(f)),
    )
    selected_audio = max(
        audio_only,
        key=lambda f: (float(f.get("abr") or 0), -size(f)),
    )
    return f"{selected_video['format_id']}+{selected_audio['format_id']}"


def _probe(path: Path, *, timeout: int = 25) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise VideoFirstError("ffprobe cannot read the downloaded media")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise VideoFirstError("ffprobe returned invalid media information") from exc
    if not isinstance(data, dict):
        raise VideoFirstError("ffprobe returned no media information")
    return data


def _streams(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        entry for entry in data.get("streams", [])
        if entry.get("codec_type") == kind
    ]


def _run_video_first(
    url: str,
    out_template: str,
    bitrate_kbps: int,
    *,
    max_height: int = 360,
    max_video_bytes: int = 350 * 1024 * 1024,
    ydl_factory: Callable[..., Any] = YoutubeDL,
) -> str:
    """Download an audio-bearing VIDEO and extract its MP3; clean video always."""
    if not 64 <= int(bitrate_kbps) <= 320:
        raise ValueError("bitrate_kbps must be between 64 and 320")
    if max_video_bytes <= 0:
        raise ValueError("max_video_bytes must be positive")

    base = out_template.replace(".%(ext)s", "")
    target = Path(f"{base}.mp3")
    video_base = Path(f"{base}-video")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise VideoFirstError("Audio output already exists; refusing to overwrite")

    pot_server = (os.getenv("MUSIX_VIDEO_FIRST_POT_PROVIDER_URL") or "").strip()
    # Same yt-dlp provider interface used by Desiders. The provider server
    # must be operated independently; this flag never starts a remote service.
    extractor_args: dict[str, dict[str, list[str]]] = {}
    if pot_server:
        from urllib.parse import urlsplit

        parsed = urlsplit(pot_server)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise VideoFirstError("Invalid self-hosted PO Token provider URL")
        extractor_args = {
            "youtubepot-bgutilhttp": {"base_url": [pot_server]},
            "youtube": {"player_client": ["mweb"], "fetch_pot": ["always"]},
        }

    try:
        options = build_ytdlp_youtube_options(
            extractor_args=extractor_args,
            skip_download=True, noplaylist=True, ignore_no_formats_error=False,
            socket_timeout=12, retries=1, fragment_retries=1,
        )
        with ydl_factory(options) as ydl:
            metadata = ydl.extract_info(url, download=False)
        if not isinstance(metadata, dict):
            raise VideoFirstError("YouTube metadata unavailable")
        fmt = _media_formats(metadata, max_height=max_height)

        options = build_ytdlp_youtube_options(
            extractor_args=extractor_args,
            format=fmt,
            outtmpl=f"{video_base}.%(ext)s",
            merge_output_format="mkv",
            max_filesize=max_video_bytes,
            socket_timeout=15,
            retries=1,
            fragment_retries=1,
        )
        with ydl_factory(options) as ydl:
            ydl.download([url])

        videos = sorted(
            (p for p in target.parent.glob(f"{video_base.name}.*") if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not videos:
            raise VideoFirstError("Downloader produced no video file")
        source = videos[0]
        if source.stat().st_size > max_video_bytes:
            raise VideoFirstError("Downloaded video exceeds size limit")

        inspected = _probe(source)
        if not _streams(inspected, "video") or not _streams(inspected, "audio"):
            raise VideoFirstError("Downloaded video has no playable audio track")

        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "2",
                "-c:a", "libmp3lame", "-b:a", f"{int(bitrate_kbps)}k",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode or not target.is_file() or target.stat().st_size == 0:
            raise VideoFirstError("FFmpeg failed to extract MP3 from downloaded video")
        if not _streams(_probe(target), "audio"):
            raise VideoFirstError("Extracted MP3 has no valid audio stream")
        return str(target)
    except (VideoFirstError, ValueError):
        target.unlink(missing_ok=True)
        raise
    except (subprocess.TimeoutExpired, OSError) as exc:
        target.unlink(missing_ok=True)
        raise VideoFirstError(f"Video-first processing failed: {type(exc).__name__}") from exc
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise VideoFirstError(
            f"Video-first acquisition failed: {type(exc).__name__}"
        ) from exc
    finally:
        for artifact in target.parent.glob(f"{video_base.name}.*"):
            if artifact.is_file():
                artifact.unlink(missing_ok=True)


async def download_video_first_mp3(
    url: str,
    out_template: str,
    bitrate_kbps: int,
) -> str:
    """Offload CPU/network work from Telegram's event loop."""
    max_mb = max(10, min(int(os.getenv("MUSIX_VIDEO_FIRST_MAX_MB", "350")), 1024))
    return await asyncio.to_thread(
        _run_video_first,
        url, out_template, bitrate_kbps,
        max_video_bytes=max_mb * 1024 * 1024,
    )
