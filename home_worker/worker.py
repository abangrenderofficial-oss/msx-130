"""MusiX experimental home YouTube-to-MP3 worker (outbound HTTPS only).

Run only on a personally controlled home PC. No inbound ports/tunnels.
Default: no YouTube cookies. Main bot falls back if this worker fails.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


def settings():
    load_dotenv(Path(__file__).with_name(".env"))
    public_url = os.getenv("MUSIX_PUBLIC_URL", "").rstrip("/")
    if not public_url.startswith("https://") or not urlparse(public_url).hostname:
        raise ValueError("MUSIX_PUBLIC_URL must be a public HTTPS address")
    for name in ("HOME_WORKER_API_TOKEN", "BOT_TOKEN", "STAGING_CHAT_ID"):
        if not os.getenv(name):
            raise ValueError(f"{name} is required")
    if len(os.environ["HOME_WORKER_API_TOKEN"]) < 32:
        raise ValueError("HOME_WORKER_API_TOKEN must be at least 32 characters")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("ffmpeg and ffprobe must be available on PATH")
    return public_url


def max_duration() -> int:
    return max(60, int(os.getenv("HOME_MAX_DURATION_SECONDS", "900")))


def max_audio_size() -> int:
    return min(49 * 1024 * 1024, int(os.getenv("HOME_MAX_AUDIO_MB", "49")) * 1024 * 1024)


def classify(error: Exception) -> str:
    value = str(error).lower()
    if "sign in to confirm" in value or "please sign in" in value or "not a bot" in value:
        return "bot_check"
    if "403" in value or "forbidden" in value:
        return "http_403"
    if "429" in value or "too many requests" in value:
        return "rate_limit"
    if "timed out" in value or "timeout" in value:
        return "timeout"
    if "duration_limit" in value:
        return "too_long"
    if "file_size_limit" in value:
        return "too_large"
    return "download_error"


def extract_audio(url: str, folder: Path):
    parsed = urlparse(url)
    if parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("Unsupported YouTube URL")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "audio.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "max_filesize": int(os.getenv("HOME_MAX_SOURCE_MB", "120")) * 1024 * 1024,
        "postprocessors": [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    }
    def guard(info, *, incomplete=False):
        duration = info.get("duration")
        if duration is not None and float(duration) > max_duration():
            return "duration_limit: longer than maximum allowed"
        size = info.get("filesize") or info.get("filesize_approx")
        if size is not None and int(size) > int(os.getenv("HOME_MAX_SOURCE_MB", "120")) * 1024 * 1024:
            return "file_size_limit: source too large"
        return None
    opts["match_filter"] = guard

    # Cookies OFF by default. Do not read youtube.txt, Brave or profile data.
    cookie_path = os.getenv("COOKIES_PATH", "").strip()
    if cookie_path:
        if not Path(cookie_path).is_file():
            raise FileNotFoundError("Configured COOKIES_PATH does not exist")
        opts["cookiefile"] = cookie_path

    # Optional JavaScript runtime; no browser or provider is required.
    if os.getenv("HOME_ENABLE_DENO", "false").lower() in {"1", "yes", "true"} and shutil.which("deno"):
        opts["js_runtimes"] = {"deno": {}}
        if os.getenv("HOME_ENABLE_REMOTE_EJS", "false").lower() in {"1", "yes", "true"}:
            opts["remote_components"] = {"ejs:github"}

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not isinstance(info, dict):
        raise RuntimeError("YouTube metadata unavailable")
    paths = list(folder.glob("*.mp3"))
    if len(paths) != 1:
        raise RuntimeError("Exactly one MP3 output required")
    target = paths[0]
    if target.stat().st_size <= 0 or target.stat().st_size > max_audio_size():
        raise ValueError("file_size_limit: MP3 exceeds Telegram limit")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-select_streams", "a:0", "-of", "default=noprint_wrappers=1:nokey=1", str(target)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    duration = float(probe.stdout.strip() or "0")
    if duration <= 0 or duration > max_duration():
        raise ValueError("duration_limit: invalid or too long MP3")
    return target, str(info.get("title") or "Audio")[:200], str(
        info.get("artist") or info.get("uploader") or "YouTube"
    )[:200], duration


def upload_staging(client: httpx.Client, file_path: Path, title: str, performer: str, duration: float) -> str:
    # Telegram file_id exists only after sendAudio to a real chat; use a private
    # staging channel, NOT the requesting user. Main bot will do final delivery.
    with file_path.open("rb") as media:
        response = client.post(
            "https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/sendAudio",
            data={
                "chat_id": os.environ["STAGING_CHAT_ID"],
                "title": title,
                "performer": performer,
                "duration": str(int(duration)),
            },
            files={"audio": (file_path.name, media, "audio/mpeg")},
            timeout=90,
        )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok") or not body.get("result", {}).get("audio", {}).get("file_id"):
        raise RuntimeError("Telegram did not return an audio file_id")
    return body["result"]["audio"]["file_id"]


def main() -> None:
    public_url = settings()
    worker_id = os.getenv("HOME_WORKER_ID", "windows-home")
    poll_interval = max(3.0, float(os.getenv("HOME_WORKER_POLL_SECONDS", "6")))
    min_delay = max(5.0, float(os.getenv("HOME_MIN_YOUTUBE_DELAY_SECONDS", "8")))
    max_per_hour = max(1, int(os.getenv("HOME_MAX_JOBS_PER_HOUR", "30")))
    cooldown_seconds = max(60, int(os.getenv("HOME_BOT_CHECK_COOLDOWN_SECONDS", "600")))
    authorization = {"Authorization": "Bearer " + os.environ["HOME_WORKER_API_TOKEN"]}
    recent: deque[float] = deque()
    last_request = 0.0
    last_heartbeat = 0.0
    cooldown_until = 0.0
    logging.info("MusiX home worker started (no cookies unless COOKIES_PATH is set)")
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=False) as client:
        while True:
            current = time.monotonic()
            while recent and recent[0] < current - 3600:
                recent.popleft()
            ready = current >= cooldown_until and len(recent) < max_per_hour
            if current - last_heartbeat >= 60 or not last_heartbeat:
                try:
                    response = client.post(
                        public_url + "/api/home-worker/heartbeat",
                        headers=authorization, json={"worker_id": worker_id, "ready": ready},
                    )
                    response.raise_for_status()
                    last_heartbeat = current
                except (httpx.HTTPError, ValueError):
                    logging.warning("Heartbeat request failed; will retry")
            if not ready:
                time.sleep(poll_interval)
                continue
            try:
                response = client.post(
                    public_url + "/api/home-worker/claim", headers=authorization,
                    json={"worker_id": worker_id},
                )
                response.raise_for_status()
                job = response.json().get("job")
            except (httpx.HTTPError, ValueError):
                logging.warning("Queue temporarily unavailable; retrying")
                time.sleep(poll_interval)
                continue
            if not job:
                time.sleep(poll_interval)
                continue
            delay = last_request + min_delay - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            started = time.monotonic()
            last_request = started
            recent.append(started)
            outcome = {
                "worker_id": worker_id,
                "job_id": job["job_id"],
                "claim_token": job["claim_token"],
                "success": False,
                "failure_reason": "download_error",
            }
            try:
                with tempfile.TemporaryDirectory(prefix="musix-home-") as folder:
                    path, title, performer, duration = extract_audio(job["url"], Path(folder))
                    file_id = upload_staging(client, path, title, performer, duration)
                    outcome.update(
                        success=True, failure_reason="", file_id=file_id,
                        title=title, performer=performer, duration=duration,
                    )
            except (DownloadError, Exception) as exc:
                outcome["failure_reason"] = classify(exc)
                logging.warning(
                    "Job failed: video_id=%s reason=%s seconds=%.1f",
                    job.get("video_id"), outcome["failure_reason"], time.monotonic()-started,
                )
                if outcome["failure_reason"] in {"bot_check", "rate_limit"}:
                    cooldown_until = time.monotonic() + cooldown_seconds
                    try:
                        client.post(
                            public_url + "/api/home-worker/heartbeat",
                            headers=authorization,
                            json={"worker_id": worker_id, "ready": False},
                            timeout=6,
                        ).raise_for_status()
                        last_heartbeat = time.monotonic()
                    except httpx.HTTPError:
                        pass
            else:
                logging.info(
                    "Job success: video_id=%s seconds=%.1f", job.get("video_id"),
                    time.monotonic() - started,
                )
            try:
                response = client.post(
                    public_url + "/api/home-worker/complete",
                    headers=authorization, json=outcome,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                logging.warning("Job completion rejected or unavailable; main bot will fall back")
            time.sleep(poll_interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Worker stopped")
