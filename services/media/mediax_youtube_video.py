"""Run MediaX's unchanged JavaScript YouTube downloader from Musix Python.

Source snapshot vendor/mediax_youtube/src copied from the *live* MediaX
commit 08e1b510. Preferred video-file download, then URL-first Telegram
delivery, then server-upload fallback matches MediaX processStandardDownload.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from aiogram import Bot
from aiogram.types import FSInputFile, Message

from services.logger import logger as logging

logging = logging.bind(service="mediax_youtube_video_bridge")
_RUNTIME = Path(__file__).resolve().parents[2] / "vendor" / "mediax_youtube"
_RESULT_MARKER = "__MUSIX_MEDIAX_YOUTUBE_RESULT__:"
_MAX_BYTES = 50 * 1024 * 1024


class MediaXVideoError(RuntimeError):
    pass


def _video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise MediaXVideoError("Expected HTTPS YouTube URL")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        segments = [s for s in parsed.path.split("/") if s]
        video_id = parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch" else (
            segments[1] if len(segments) > 1 and segments[0] in ("shorts", "embed", "live") else ""
        )
    elif host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        raise MediaXVideoError("Only YouTube video URLs accepted")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise MediaXVideoError("Invalid YouTube video ID")
    return video_id


def _node_path() -> str:
    candidates = [
        shutil.which("node"),
        str(Path.cwd() / ".local" / "bin" / "node"),
        "/home/container/.local/bin/node",
    ]
    try:
        import nodejs_wheel
        candidates.append(str(Path(nodejs_wheel.__file__).resolve().parent / "bin" / "node"))
    except ImportError:
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise MediaXVideoError("Node.js is unavailable; install nodejs-wheel runtime")


def _run_original_mediax(url: str) -> dict:
    _video_id(url)
    if not (_RUNTIME / "runner.mjs").is_file():
        raise MediaXVideoError("MediaX source snapshot not deployed")
    env = dict(os.environ)
    env.update({
        "MUSIX_MEDIA_PYTHON": sys.executable,
        "MUSIX_MEDIA_FFMPEG": shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
    })
    try:
        result = subprocess.run(
            [_node_path(), str(_RUNTIME / "runner.mjs"), url],
            cwd=_RUNTIME,
            env=env,
            capture_output=True,
            text=True,
            timeout=185,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaXVideoError("MediaX YouTube pipeline timed out") from exc
    matches = [
        line[len(_RESULT_MARKER):]
        for line in result.stdout.splitlines()
        if line.startswith(_RESULT_MARKER)
    ]
    if not matches:
        # Avoid logging external stream URLs and secrets returned by extractors.
        raise MediaXVideoError(
            f"MediaX runtime failed exit={result.returncode} "
            f"stderr_tail={result.stderr[-300:]}"
        )
    data = json.loads(matches[-1])
    if data.get("mediaxRevision") != "08e1b510dfa7396d6b1d05561918db30c604f0c7":
        raise MediaXVideoError("Unexpected MediaX source revision")
    return data


def _safe_source_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if host == "localhost" or host.endswith((".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _verify_video(path: str) -> None:
    if not Path(path).is_file() or Path(path).stat().st_size < 2048:
        raise MediaXVideoError("Downloaded file is empty or invalid")
    check = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name:format=duration", "-of", "json", path],
        capture_output=True,
        text=True,
        timeout=14,
        check=True,
    )
    data = json.loads(check.stdout)
    types = {stream.get("codec_type") for stream in data.get("streams") or []}
    if not {"video", "audio"}.issubset(types):
        raise MediaXVideoError("Downloaded video lacks audio or video track")
    if float((data.get("format") or {}).get("duration") or 0) <= 0:
        raise MediaXVideoError("Downloaded video has no playable duration")


def _cleanup_mediax_local_file(file_path: str) -> None:
    # MediaX's original local upload creates /tmp/ar-youtube-UUID.ext.
    path = Path(file_path)
    if path.parent != Path(tempfile.gettempdir()) or not re.match(
        r"^ar-youtube-[a-f0-9-]{30,50}\.(mp4|mkv|webm)$", path.name
    ):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logging.warning("MediaX temp video could not be removed")


async def _server_upload_from_url(
    bot: Bot, chat_id: int, video: dict, *, max_bytes: int
) -> Message:
    source = str(video.get("url") or "")
    if not _safe_source_url(source):
        raise MediaXVideoError("Unsafe provider stream URL")
    headers = dict(video.get("headers") or {})
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(48.0, connect=12.0),
        follow_redirects=True,
    ) as client:
        with tempfile.TemporaryDirectory(prefix="musix-mediax-video-") as tmp:
            file_path = str(Path(tmp) / "source.mp4")
            count = 0
            async with client.stream("GET", source, headers=headers) as response:
                response.raise_for_status()
                if not _safe_source_url(str(response.url)):
                    raise MediaXVideoError("Provider stream redirected unsafely")
                content_length = int(response.headers.get("content-length") or 0)
                if content_length > max_bytes:
                    raise MediaXVideoError("Video larger than Telegram limit")
                kind = response.headers.get("content-type", "").lower()
                if "text/html" in kind or "application/json" in kind:
                    raise MediaXVideoError("Provider returned HTML/JSON, not media")
                with open(file_path, "wb") as output:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        count += len(chunk)
                        if count > max_bytes:
                            raise MediaXVideoError("Video exceeded Telegram size limit")
                        output.write(chunk)
            _verify_video(file_path)
            sent = await bot.send_video(
                chat_id=chat_id,
                video=FSInputFile(file_path, filename="musix-mediax-youtube.mp4"),
                supports_streaming=True,
            )
            if not sent.video:
                raise MediaXVideoError("Telegram did not return a playable video")
            return sent


async def send_mediax_youtube_video(bot: Bot, chat_id: int, url: str) -> dict:
    """MediaX exact extraction path, original URL-first Telegram delivery.

    Does NOT download MP3. This proves the first video stage in isolation.
    """
    _video_id(url)
    result = await asyncio.to_thread(_run_original_mediax, url)
    errors = []
    if result.get("status") == "local_file":
        path = str(result.get("filePath") or "")
        try:
            _verify_video(path)
            sent = await bot.send_video(
                chat_id=chat_id,
                video=FSInputFile(path),
                supports_streaming=True,
            )
            if not sent.video:
                raise MediaXVideoError("Telegram accepted local file without video object")
            return {
                "strategy": "mediax-local-video-upload",
                "message_id": sent.message_id,
                "file_id": sent.video.file_id,
                "quality": result.get("quality"),
                "size": sent.video.file_size,
            }
        except Exception as exc:
            errors.append(f"local-file:{type(exc).__name__}:{str(exc)[-160:]}")
        finally:
            _cleanup_mediax_local_file(path)

    for candidate in (result.get("candidates") or [])[:6]:
        url_string = str(candidate.get("url") or "")
        if not _safe_source_url(url_string):
            continue
        # EXACT MediaX preference: let Telegram fetch stream URL directly.
        # That can work when the bot host IP is blocked.
        try:
            sent = await bot.send_video(
                chat_id=chat_id,
                video=url_string,
                supports_streaming=True,
            )
            if sent.video:
                return {
                    "strategy": "mediax-telegram-sendVideo-url",
                    "message_id": sent.message_id,
                    "file_id": sent.video.file_id,
                    "quality": candidate.get("quality"),
                    "size": sent.video.file_size,
                }
            errors.append("telegram-url:no-video-object")
        except Exception as exc:
            errors.append(f"telegram-url:{type(exc).__name__}:{str(exc)[-160:]}")

        # EXACT MediaX fallback when Telegram's URL fetch fails: host downloads
        # and uploads the file itself. Uploaded bytes are validated with ffprobe.
        try:
            sent = await _server_upload_from_url(
                bot, chat_id, candidate, max_bytes=_MAX_BYTES,
            )
            return {
                "strategy": "mediax-server-video-upload",
                "message_id": sent.message_id,
                "file_id": sent.video.file_id,
                "quality": candidate.get("quality"),
                "size": sent.video.file_size,
            }
        except Exception as exc:
            errors.append(f"server-upload:{type(exc).__name__}:{str(exc)[-160:]}")

    raise MediaXVideoError(
        "MediaX YouTube video delivery exhausted; "
        f"resolver={str(result.get('resolverError') or '')[-130:]} "
        f"upload={str(result.get('uploadError') or '')[-130:]} "
        + " | ".join(errors[-5:])
    )
