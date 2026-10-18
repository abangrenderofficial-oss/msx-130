"""Musix-only video-to-audio fallback, inspired by MediaX's yt-dlp/FFmpeg route.

Works on videos the bot is permitted to access. No MediaX imports or shared bot
state. This is the first YouTube download attempt; legacy fallbacks remain available.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from typing import Any

from yt_dlp import YoutubeDL

from services.logger import logger as logging
from services.platforms.youtube_media import build_ytdlp_youtube_options

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
    standard = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    allowed = [rate for rate in standard if rate <= source_abr + 2 and rate <= safe_requested]
    return max(allowed) if allowed else 32


def _cleanup_outputs(out_template: str) -> None:
    prefix = out_template.replace(".%(ext)s", "")
    for filename in glob.glob(prefix + ".*"):
        if os.path.isfile(filename):
            os.remove(filename)


def _verified_mp3(out_template: str) -> str:
    filename = out_template.replace(".%(ext)s", ".mp3")
    if not os.path.isfile(filename) or os.path.getsize(filename) < 2048:
        raise RuntimeError("Video-to-audio fallback produced no usable MP3 file")
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name:format=duration",
                "-of", "json", filename,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=12, check=True, text=True,
        )
    except FileNotFoundError:
        # Some small bot hosts bundle FFmpeg without ffprobe. Mutagen is
        # already a pinned Musix dependency; never accept unchecked bytes.
        try:
            from mutagen.mp3 import MP3

            info = MP3(filename).info
            if (info.length or 0) <= 0:
                raise ValueError("MP3 has no duration")
        except Exception as exc:
            raise RuntimeError("Fallback MP3 failed Mutagen validation") from exc
        return filename

    data = json.loads(probe.stdout)
    streams = data.get("streams") or []
    duration = _positive_number((data.get("format") or {}).get("duration"))
    if not streams or streams[0].get("codec_name") != "mp3" or not duration:
        raise RuntimeError("Video-to-audio fallback output failed audio validation")
    return filename


def _downloaded_video_path(video_template: str, max_bytes: int) -> str:
    prefix = video_template.replace(".%(ext)s", "")
    candidates = sorted(
        path for path in glob.glob(prefix + ".*")
        if os.path.isfile(path)
        and os.path.splitext(path)[1].lower() in {".mp4", ".mkv", ".webm", ".mov"}
    )
    for path in candidates:
        size = os.path.getsize(path)
        if 2048 <= size <= max_bytes:
            return path
    raise RuntimeError("YouTube video download did not produce a complete usable video")


def _verified_video(path: str) -> None:
    """Prove the downloaded input has *both* video and real audio."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name:format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True, timeout=15,
    )
    data = json.loads(result.stdout)
    codecs = {str(item.get("codec_type")) for item in data.get("streams", [])}
    if not {"video", "audio"}.issubset(codecs):
        raise RuntimeError("Downloaded YouTube video has no video+audio streams")
    if not _positive_number((data.get("format") or {}).get("duration")):
        raise RuntimeError("Downloaded YouTube video has no valid duration")


def _extract_downloaded_video_to_mp3(video_path: str, out_template: str, bitrate: int) -> str:
    target = out_template.replace(".%(ext)s", ".mp3")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-y", "-i", video_path, "-map", "0:a:0", "-vn",
         "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", target],
        capture_output=True, text=True, timeout=180, check=True,
    )
    return _verified_mp3(out_template)


def download_youtube_video_as_mp3(
    url: str, out_template: str, *, bitrate_kbps: int
) -> str:
    """Video FIRST: download actual media file, verify audio, *then* extract MP3.

    Unlike a yt-dlp FFmpegExtractAudio postprocessor, the original video file
    remains available for validation before conversion. The video is removed
    after processing. A YouTube login/IP block still prevents the download.
    """
    max_bytes = _source_limit()
    video_template = out_template.replace(".%(ext)s", "-video.%(ext)s")
    metadata_opts = build_ytdlp_youtube_options(
        skip_download=True, ignore_no_formats_error=True, noplaylist=True,
        quiet=True, socket_timeout=12, retries=1,
    )
    formats: list[dict[str, Any]] = []
    try:
        with YoutubeDL(metadata_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("YouTube returned no format metadata")
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise RuntimeError("Live video is not supported")
        formats = select_video_audio_formats(
            info.get("formats") or [], max_bytes=max_bytes,
        )
    except Exception as exc:
        logging.warning("Video format metadata unavailable: error=%s", str(exc)[-200:])

    attempts: list[tuple[str, float | None, str]] = [
        (str(fmt["format_id"]), _positive_number(fmt.get("abr")), f'muxed:{fmt["format_id"]}')
        for fmt in formats[:4]
    ]
    # Same yt-dlp merging method that MediaX uses when progressive video+audio
    # is not available. Both video and audio streams must be downloadable.
    attempts.append((
        "bestvideo[height<=480]+bestaudio[ext=m4a]/"
        "bestvideo[height<=480]+bestaudio/"
        "bestvideo[height<=720]+bestaudio",
        None, "mediax-style:video+audio",
    ))
    errors: list[str] = []
    for selector, source_abr, label in attempts:
        _cleanup_outputs(video_template)
        _cleanup_outputs(out_template)
        options = build_ytdlp_youtube_options(
            format=selector, outtmpl=video_template, merge_output_format="mkv",
            noplaylist=True, quiet=True, no_warnings=True,
            socket_timeout=15, retries=1, fragment_retries=1,
            max_filesize=max_bytes,
        )
        try:
            # STEP 1: Download VIDEO, with audio, to a real temporary file.
            with YoutubeDL(options) as ydl:
                status = ydl.download([url])
            if status not in (0, None):
                raise RuntimeError(f"Video download failed: yt-dlp exit {status}")
            video_path = _downloaded_video_path(video_template, max_bytes)
            # STEP 2: Reject missing/silent/fake media before conversion.
            _verified_video(video_path)
            logging.info(
                "MUSIX_VIDEO_DOWNLOAD_OK: bytes=%s strategy=%s",
                os.path.getsize(video_path), label,
            )
            # STEP 3: Extract audio ONLY after full downloaded-video validation.
            output_kbps = _mp3_bitrate_kbps(source_abr, bitrate_kbps)
            mp3_path = _extract_downloaded_video_to_mp3(
                video_path, out_template, output_kbps,
            )
            logging.info(
                "MUSIX_VIDEO_TO_MP3_OK: strategy=%s output_kbps=%s mp3_bytes=%s",
                label, output_kbps, os.path.getsize(mp3_path),
            )
            return mp3_path
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[-180:]}")
            logging.warning(
                "MUSIX_VIDEO_DOWNLOAD_OR_CONVERT_FAIL: strategy=%s error_type=%s",
                label, type(exc).__name__,
            )
        finally:
            _cleanup_outputs(video_template)
    # MediaX doesn't always need yt-dlp: its resolver obtains progressive
    # video URLs from independent providers. Musix MUST download those bytes
    # to disk before FFmpeg (a Telegram URL-send is not a local video file).
    try:
        from services.media.youtube_mediax_provider import (
            download_video_from_mediax_providers,
        )

        provider_path, provider_name = download_video_from_mediax_providers(
            url, video_template, max_bytes,
        )
        _verified_video(provider_path)
        logging.info(
            "MUSIX_VIDEO_DOWNLOAD_OK: bytes=%s strategy=provider:%s",
            os.path.getsize(provider_path), provider_name,
        )
        mp3_path = _extract_downloaded_video_to_mp3(
            provider_path, out_template, _mp3_bitrate_kbps(None, bitrate_kbps),
        )
        logging.info(
            "MUSIX_VIDEO_TO_MP3_OK: strategy=provider:%s mp3_bytes=%s",
            provider_name, os.path.getsize(mp3_path),
        )
        return mp3_path
    except Exception as provider_exc:
        errors.append(
            f"mediax-provider:{type(provider_exc).__name__}: "
            f"{str(provider_exc)[-180:]}"
        )
        logging.warning(
            "MUSIX_MEDIA_PROVIDER_VIDEO_FAILED: error_type=%s",
            type(provider_exc).__name__,
        )
    finally:
        _cleanup_outputs(video_template)

    _cleanup_outputs(out_template)
    raise RuntimeError("Video-first attempts exhausted: " + " | ".join(errors[-6:]))
