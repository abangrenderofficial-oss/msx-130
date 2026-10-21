"""Bright Data direct SABR/UMP audio fallback with smart source bitrate selection.

No audio captureStream / playback recording. Disabled unless explicitly enabled.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_LOG = logging.getLogger(__name__)
_YOUTUBE_ID = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_AUDIO_MIME = ("audio/webm", "audio/mp4")
_SEEK_SPACING = 11
_MAX_RUNTIME_SEC = 150
_MAX_SOURCE_BYTES = 52 * 1024 * 1024
_BROWSER_MUTEX = asyncio.Semaphore(1)


class DirectAudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioFormat:
    itag: int
    bitrate: int
    mime: str
    content_length: int
    duration_ms: int

    @property
    def codec(self) -> str:
        return "opus" if "opus" in self.mime else "aac" if "mp4a" in self.mime else "unknown"


def ranked_audio_formats(formats: list[dict]) -> list[AudioFormat]:
    """Sort audio-only formats by *source* bitrate, with stable codec preference for ties."""
    candidates = []
    for entry in formats:
        if not isinstance(entry, dict):
            continue
        mime = str(entry.get("mimeType") or "")
        if not mime.startswith(_AUDIO_MIME):
            continue
        try:
            itag = int(entry["itag"])
            bitrate = int(entry.get("averageBitrate") or entry.get("bitrate") or 0)
            content_length = int(entry.get("contentLength") or 0)
            duration_ms = int(entry.get("approxDurationMs") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if itag < 1 or bitrate <= 0 or not 0 < content_length < _MAX_SOURCE_BYTES:
            continue
        candidates.append(AudioFormat(itag, bitrate, mime, content_length, duration_ms))
    unique: dict[int, AudioFormat] = {}
    for fmt in candidates:
        if fmt.itag not in unique or fmt.bitrate > unique[fmt.itag].bitrate:
            unique[fmt.itag] = fmt
    return sorted(unique.values(), key=lambda f: (f.bitrate, f.codec == "opus", f.itag), reverse=True)


def _read_ump_number(stream: io.BytesIO) -> int | None:
    """UMP custom size-prefixed integer, as used by YouTube's media stream."""
    raw = stream.read(1)
    if not raw:
        return None
    prefix = raw[0]
    ones = 0
    for bit in range(7, 3, -1):
        if prefix & (1 << bit):
            ones += 1
        else:
            break
    size = min(ones + 1, 5)
    shift = 0 if size == 5 else 8 - size
    result = 0 if size == 5 else prefix & ((1 << shift) - 1)
    for _ in range(size - 1):
        raw = stream.read(1)
        if not raw:
            raise DirectAudioError("Truncated UMP number")
        result |= raw[0] << shift
        shift += 8
    return result


def _ump_parts(payload: bytes):
    stream = io.BytesIO(payload)
    while stream.tell() < len(payload):
        part_type = _read_ump_number(stream)
        if part_type is None:
            break
        size = _read_ump_number(stream)
        if size is None or size > len(payload) - stream.tell():
            raise DirectAudioError("Invalid UMP packet length")
        yield part_type, stream.read(size)


def _read_pb_varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    for n in range(10):
        if index >= len(data):
            raise DirectAudioError("Truncated protobuf field")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << (n * 7)
        if not byte & 0x80:
            return result, index
    raise DirectAudioError("Malformed protobuf integer")


def _media_header_fields(data: bytes) -> dict[int, int]:
    """Read only integer fields needed: header_id=1, itag=3, sequence_number=9."""
    out: dict[int, int] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_pb_varint(data, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_pb_varint(data, pos)
            if field in (1, 3, 9):
                out[field] = value
        elif wire == 2:
            length, pos = _read_pb_varint(data, pos)
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise DirectAudioError("Unsupported media header protobuf wire format")
        if pos > len(data):
            raise DirectAudioError("Truncated media header")
    return out


def parse_ump_audio(payload: bytes) -> dict[tuple[int, int], bytes]:
    """Extract compressed source audio segments. No decode or re-encode."""
    headers: dict[int, tuple[int, int]] = {}
    segments: dict[tuple[int, int], bytearray] = defaultdict(bytearray)
    for part_type, data in _ump_parts(payload):
        if part_type == 20:  # MEDIA_HEADER
            info = _media_header_fields(data)
            if 1 in info and 3 in info:
                headers[info[1]] = (info[3], info.get(9, 0))
        elif part_type == 21 and data:  # MEDIA
            header = headers.get(data[0])
            if header:
                segments[header].extend(data[1:])
    return {key: bytes(data) for key, data in segments.items() if data}


def choose_downloaded_audio(
    offered_formats: list[dict],
    collected: dict[tuple[int, int], bytes],
) -> tuple[AudioFormat, bytes]:
    """Pick highest-bitrate fully downloaded source; reject incomplete streams."""
    ranked = ranked_audio_formats(offered_formats)
    if not ranked:
        raise DirectAudioError("Player returned no usable audio source formats")
    for fmt in ranked:
        segments = {seq: data for (itag, seq), data in collected.items() if itag == fmt.itag}
        if not segments or 0 not in segments:
            continue
        last = max(segments)
        if set(segments) != set(range(last + 1)):
            continue
        raw = b"".join(segments[seq] for seq in range(last + 1))
        if len(raw) == fmt.content_length:
            return fmt, raw
    available = sorted({itag for itag, _ in collected})
    raise DirectAudioError(f"No complete audio stream found; captured itags={available}")


def _video_id(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        from urllib.parse import parse_qs
        value = parse_qs(parsed.query).get("v", [""])[0]
        if not value:
            parts = parsed.path.strip("/").split("/")
            value = parts[-1] if len(parts) == 2 and parts[0] in ("shorts", "live", "embed") else ""
    elif host in ("youtu.be", "www.youtu.be"):
        value = parsed.path.strip("/").split("/")[0]
    else:
        value = ""
    if not _YOUTUBE_ID.fullmatch(value):
        raise DirectAudioError("Not a valid YouTube video URL")
    return value


def _browser_connection() -> tuple[str, str]:
    raw = (os.getenv("BRIGHTDATA_BROWSER_WS") or "").strip()
    parts = urlsplit(raw)
    if parts.scheme != "wss" or parts.hostname != "brd.superproxy.io" or not parts.username or not parts.password:
        raise DirectAudioError("Bright Data Browser API is not configured")
    name = unquote(parts.username)
    password = unquote(parts.password)
    if not name.startswith("brd-customer-") or not password or set(password) == {"*"}:
        raise DirectAudioError("Invalid Bright Data Browser API credentials")
    creds = base64.b64encode(f"{name}:{password}".encode()).decode()
    return f"wss://{parts.hostname}:{parts.port or 9222}", creds


def _claim_paid_browser_session() -> None:
    """Persistent per-UTC-day safety limit; failures also count as billable sessions."""
    daily_limit = max(0, min(int(os.getenv("MUSIX_BRIGHTDATA_MAX_DAILY_SESSIONS", "5")), 200))
    state_file = Path(".musix_brightdata_daily_usage.json")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    used = int(state.get("count") or 0) if state.get("date") == today else 0
    if used >= daily_limit:
        raise DirectAudioError("Bright Data daily credit safety limit reached")
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"date": today, "count": used + 1}), encoding="utf-8")
    tmp.replace(state_file)


async def _fetch_best_audio(video_url: str) -> tuple[AudioFormat, bytes, str, float]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise DirectAudioError("Playwright dependency unavailable") from exc
    ws, token = _browser_connection()
    video_id = _video_id(video_url)
    offer: list[dict] = []
    title = ""
    duration = 0.0
    responses: list[asyncio.Task] = []
    records: dict[tuple[int, int], bytes] = {}

    async with _BROWSER_MUTEX:
        _claim_paid_browser_session()
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(
                ws, headers={"Authorization": f"Basic {token}"}, timeout=30000
            )
            try:
                page = await browser.new_page()
                page.set_default_navigation_timeout(80000)
                async def filter_resources(route):
                    if route.request.resource_type in ("font", "image", "stylesheet"):
                        await route.abort()
                    else:
                        await route.continue_()
                await page.route("**/*", filter_resources)
                async def on_media(response):
                    if response.status != 200 or "application/vnd.yt-ump" not in response.headers.get("content-type", ""):
                        return
                    try:
                        payload = await asyncio.wait_for(response.body(), 24)
                        if len(payload) > 12 * 1024 * 1024:
                            return
                        for key, chunk in parse_ump_audio(payload).items():
                            if len(chunk) > len(records.get(key, b"")):
                                records[key] = chunk
                    except (asyncio.TimeoutError, DirectAudioError) as exc:
                        _LOG.debug("Bright Data UMP response skipped: %s", type(exc).__name__)
                page.on("response", lambda response: responses.append(asyncio.create_task(on_media(response))))
                result = await page.goto(
                    f"https://www.youtube.com/watch?v={video_id}",
                    wait_until="domcontentloaded",
                    timeout=80000,
                )
                if not result or result.status != 200:
                    raise DirectAudioError("YouTube page could not be loaded")
                await page.wait_for_timeout(2900)
                info = await page.evaluate("""() => {
                    const p=window.ytInitialPlayerResponse;
                    const v=document.querySelector('video');
                    if(v)v.pause();
                    return {
                        title:document.title,
                        status:p?.playabilityStatus?.status,
                        formats:p?.streamingData?.adaptiveFormats||[],
                        duration:Number(v?.duration||p?.videoDetails?.lengthSeconds||0),
                    }
                }""")
                if info.get("status") != "OK":
                    raise DirectAudioError("YouTube player unavailable: " + repr(info.get("status")) + " title=" + repr(str(info.get("title"))[:90]))
                title = str(info.get("title") or "")
                duration = float(info.get("duration") or 0)
                if not 0 < duration <= 1200:
                    raise DirectAudioError("Unsupported video duration (limit: 20 minutes)")
                offer = info.get("formats") or []
                ranked = ranked_audio_formats(offer)
                if not ranked:
                    raise DirectAudioError("No ranked audio formats available")
                if ranked[0].content_length > _MAX_SOURCE_BYTES:
                    raise DirectAudioError("Source audio exceeds byte limit")
                # Seeking forces SABR to fetch source chunks; no playback or recording.
                for second in range(_SEEK_SPACING, max(_SEEK_SPACING, int(duration)), _SEEK_SPACING):
                    if second >= duration - 0.4:
                        break
                    await page.evaluate("""async(t) => {
                        const v=document.querySelector('video');
                        if(!v)return;
                        v.pause();v.currentTime=t;
                        await Promise.race([
                           new Promise(resolve=>v.addEventListener('seeked',resolve,{once:true})),
                           new Promise(resolve=>setTimeout(resolve,3500))
                        ]);
                    }""", second)
                    await page.wait_for_timeout(700)
                    if any(
                        sum(len(data) for (itag,_),data in records.items() if itag == f.itag) == f.content_length
                        for f in ranked
                    ):
                        break
                await page.wait_for_timeout(2400)
                if responses:
                    await asyncio.gather(*responses, return_exceptions=True)
            finally:
                await browser.close()
    fmt, raw = choose_downloaded_audio(offer, records)
    _LOG.info(
        "Bright Data smart source selected: itag=%s codec=%s bitrate=%s source_bytes=%s best_offered_itag=%s",
        fmt.itag, fmt.codec, fmt.bitrate, len(raw), ranked_audio_formats(offer)[0].itag,
    )
    return fmt, raw, title, duration


async def download_best_audio_mp3(
    url: str, out_template: str, bitrate_kbps: int,
) -> str:
    """Feature-gated external entry point used by Musix's fallback chain."""
    base = out_template.replace(".%(ext)s", "")
    source = Path(f"{base}-brightdata-source.bin")
    target = Path(f"{base}.mp3")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise DirectAudioError("Refusing to overwrite an existing MP3")
    try:
        fmt, raw, title, duration = await asyncio.wait_for(_fetch_best_audio(url), _MAX_RUNTIME_SEC)
        source.write_bytes(raw)
        inspection = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_name,codec_type", "-of", "json", str(source)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if inspection.returncode:
            raise DirectAudioError("Downloaded source audio failed ffprobe")
        parsed = json.loads(inspection.stdout)
        streams = parsed.get("streams") or []
        if not any(s.get("codec_type") == "audio" for s in streams):
            raise DirectAudioError("Downloaded source has no audio stream")
        decoded_duration = float((parsed.get("format") or {}).get("duration") or 0)
        if abs(decoded_duration - duration) > 1.3:
            raise DirectAudioError("Downloaded source audio duration is incomplete")
        bitrate_kbps = max(128, min(int(bitrate_kbps), 320))
        conversion = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", str(source), "-map", "0:a:0", "-vn",
             "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", str(target)],
            capture_output=True, text=True, timeout=75, check=False,
        )
        if conversion.returncode or not target.exists() or target.stat().st_size < 10000:
            raise DirectAudioError("FFmpeg failed to produce MP3 from source stream")
        _LOG.info(
            "Bright Data MP3 ready: title=%s selected_itag=%s source_kbps=%s output_kbps=%s duration=%ss",
            title[:70], fmt.itag, round(fmt.bitrate/1000), bitrate_kbps, round(duration,1),
        )
        return str(target)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        source.unlink(missing_ok=True)
