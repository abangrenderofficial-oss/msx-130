"""Musix-only video-to-audio fallback, inspired by MediaX's yt-dlp/FFmpeg route.

Works on videos the bot is permitted to access. No MediaX imports or shared bot
state. This is a fallback; it does not replace the existing audio downloader.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from services.logger import logger as logging
from services.platforms.youtube_media import build_ytdlp_youtube_options
from services.platforms.ytdlp_helpers import mp3_extract_postprocessors

logging = logging.bind(service="musix_video_audio_fallback")


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
        if 0 < number < float("inf"):
            return number
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _source_limit() -> int:
    # Prevent accidental multi-hundred-MB video downloads on budget hosting.
    requested_mb = _positive_number(os.getenv("MUSIX_YOUTUBE_VIDEO_AUDIO_MAX_MB")) or 150
    return int(min(max(requested_mb, 10), 500) * 1024 * 1024)


def select_video_audio_formats(
    formats: list[dict[str, Any]], *, max_bytes: int | None = None
) -> list[dict[str, Any]]:
    """Rank *muxed* formats: audio quality first, minimum download size second.

    Total bitrate (tbr) is NOT audio bitrate. Formats with unknown abr are
    ranked after known abr; no audio bitrate is inferred from resolution.
    Different audio codecs are not strictly comparable from abr alone.
    """
    limit = _source_limit() if max_bytes is None else max_bytes
    candidates = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if not fmt.get("format_id") or fmt.get("has_drm") or fmt.get("is_drm"):
            continue
        if str(fmt.get("vcodec") or "none") == "none":
            continue
        if str(fmt.get("acodec") or "none") == "none":
            continue
        if fmt.get("protocol") == "mhtml":
            continue
        size = _positive_number(fmt.get("filesize")) or _positive_number(
            fmt.get("filesize_approx")
        )
        if size is not None and size > limit:
            continue
        candidates.append(fmt)

    def ranking(fmt: dict[str, Any]) -> tuple[float, int, float, float, str]:
        audio_kbps = _positive_number(fmt.get("abr"))
        size = _positive_number(fmt.get("filesize")) or _positive_number(
            fmt.get("filesize_approx")
        )
        height = _positive_number(fmt.get("height")) or 100000
        return (
            -(audio_kbps or 0),
            0 if size is not None else 1,
            size if size is not None else float("inf"),
            height,
            str(fmt.get("format_id")),
        )

    return sorted(candidates, key=ranking)


def _mp3_bitrate_kbps(source_abr: float | None, requested: int) -> int:
    """Avoid wasteful 320-kbps output from a known low-bitrate source."""
    safe_requested = max(64, min(int(requested), 320))
    if source_abr is None:
        return min(safe_requested, 160)
    # Encode to a conventional rate not significantly above source bitrate.
    standard = (64, 96, 128, 160, 192, 224, 256, 320)
    allowed = [rate for rate in standard if rate <= source_abr + 2 and rate <= safe_requested]
    return max(allowed) if allowed else 64


def _cleanup_outputs(out_template: str) -> None:
    prefix = out_template.replace(".%(ext)s", "")
    for filename in glob.glob(prefix + ".*"):
        if os.path.isfile(filename):
            os.remove(filename)


def _verified_mp3(out_template: str) -> str:
    filename = out_template.replace(".%(ext)s", ".mp3")
    if not os.path.isfile(filename) or os.path.getsize(filename) < 2048:
        raise RuntimeError("Video-to-audio fallback produced no usable MP3 file")
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name:format=duration",
            "-of", "json", filename,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=12, check=True, text=True,
    )
    data = json.loads(probe.stdout)
    streams = data.get("streams") or []
    duration = _positive_number((data.get("format") or {}).get("duration"))
    if not streams or streams[0].get("codec_name") != "mp3" or not duration:
        raise RuntimeError("Video-to-audio fallback output failed audio validation")
    return filename


def download_youtube_video_as_mp3(
    url: str, out_template: str, *, bitrate_kbps: int
) -> str:
    """Scan available muxed formats; extract audio from the smallest suitable video.

    If no muxed format can be downloaded, try a MediaX-style low-resolution
    video + best audio merge. This does NOT bypass YouTube authentication,
    download permissions, DRM, or IP restrictions.
    """
    max_bytes = _source_limit()
    metadata_opts = build_ytdlp_youtube_options(
        skip_download=True,
        ignore_no_formats_error=True,
        noplaylist=True,
        quiet=True,
        socket_timeout=12,
        retries=1,
    )
    formats: list[dict[str, Any]] = []
    try:
        with YoutubeDL(metadata_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("YouTube returned no format metadata")
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise RuntimeError("Live video is not supported")
        formats = select_video_audio_formats(info.get("formats") or [], max_bytes=max_bytes)
    except Exception as exc:
        # Even if metadata is blocked, allow one direct MediaX-style attempt.
        logging.warning("Muxed format scan unavailable: error=%s", str(exc)[-200:])

    # Keep format IDs from a single probe. Avoid downloading every resolution.
    attempts: list[tuple[str, float | None, str]] = []
    for fmt in formats[:4]:
        attempts.append((
            str(fmt["format_id"]),
            _positive_number(fmt.get("abr")),
            f'muxed:{fmt["format_id"]}',
        ))
    # For videos whose audio is exposed only separately, mirror the MediaX
    # ffmpeg merge workflow at the lowest practical video size.
    attempts.append((
        "bestvideo[height<=480]+bestaudio[ext=m4a]/"
        "bestvideo[height<=480]+bestaudio/"
        "bestvideo[height<=720]+bestaudio",
        None,
        "mediax-style:video+audio",
    ))

    errors: list[str] = []
    for format_selector, source_abr, label in attempts:
        _cleanup_outputs(out_template)
        output_kbps = _mp3_bitrate_kbps(source_abr, bitrate_kbps)
        options = build_ytdlp_youtube_options(
            format=format_selector,
            outtmpl=out_template,
            postprocessors=mp3_extract_postprocessors(str(output_kbps)),
            merge_output_format="mkv",
            noplaylist=True,
            quiet=True,
            no_warnings=True,
            socket_timeout=15,
            retries=1,
            fragment_retries=1,
            max_filesize=max_bytes,
        )
        try:
            with YoutubeDL(options) as ydl:
                result = ydl.download([url])
            if result not in (0, None):
                raise RuntimeError(f"yt-dlp exit status {result}")
            filename = _verified_mp3(out_template)
            logging.info(
                "Video-to-audio succeeded: strategy=%s output_kbps=%s source_abr=%s bytes=%s",
                label, output_kbps, source_abr, os.path.getsize(filename),
            )
            return filename
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[-180:]}")
            logging.warning(
                "Video-to-audio candidate failed: strategy=%s error_type=%s",
                label, type(exc).__name__,
            )
    _cleanup_outputs(out_template)
    raise RuntimeError("Video-to-audio exhausted: " + " | ".join(errors[-5:]))
