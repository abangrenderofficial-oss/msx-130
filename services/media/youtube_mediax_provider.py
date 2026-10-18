"""Musix-only copy of MediaX's alternative YouTube video source strategy.

Provider endpoints can be unavailable or rate-limited. No cookies/accounts
are forwarded; all downloaded bytes are later validated with FFprobe.
"""
from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx

from services.logger import logger as logging

logging = logging.bind(service="musix_youtube_mediax_provider")

INVIDIOUS_INSTANCES = (
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yt.chocolatemoo53.com",
    "https://invidious.tiekoetter.com",
)
PIPED_INSTANCES = (
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
)
USER_AGENT = "Mozilla/5.0 (compatible; Musix/1.0)"
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _youtube_id(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Invalid YouTube URL scheme")
    if hostname in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        chunks = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif chunks and chunks[0] in ("shorts", "embed", "live") and len(chunks) > 1:
            video_id = chunks[1]
        else:
            video_id = ""
    elif hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        raise ValueError("Not a YouTube hostname")
    if not YT_ID.fullmatch(video_id):
        raise ValueError("Not a valid YouTube video id")
    return video_id


def _stream_url_ok(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global
        except ValueError:
            return True
    except ValueError:
        return False


def _receive(client: httpx.Client, source_url: str, dest: str, max_bytes: int) -> str:
    """Stream to disk with a hard byte ceiling; never buffer the entire video."""
    if not _stream_url_ok(source_url):
        raise ValueError("Provider returned an invalid or unsafe stream URL")
    written = 0
    with client.stream("GET", source_url) as response:
        response.raise_for_status()
        if not _stream_url_ok(str(response.url)):
            raise ValueError("Provider redirected to an unsafe stream URL")
        kind = (response.headers.get("content-type") or "").lower()
        if "text/html" in kind or "application/json" in kind:
            raise ValueError("Provider returned an error page, not video")
        length = response.headers.get("content-length")
        if length and int(length) > max_bytes:
            raise ValueError("Provider video exceeds source download limit")
        with open(dest, "wb") as output:
            for chunk in response.iter_bytes(chunk_size=256 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("Provider video exceeded source download limit")
                output.write(chunk)
    if written < 2048:
        raise ValueError("Provider returned too few bytes for a real video")
    return dest


def download_video_from_mediax_providers(
    url: str, out_template: str, max_bytes: int
) -> tuple[str, str]:
    """Try the same external provider *classes* as MediaX.

    Video is locally downloaded for FFmpeg instead of sending the provider
    URL directly to Telegram (MediaX can do that for video delivery).
    """
    video_id = _youtube_id(url)
    base = out_template.replace(".%(ext)s", "")
    destination = base + ".mp4"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    errors: list[str] = []

    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout(22.0, connect=8.0),
        follow_redirects=True,
    ) as client:
        # Same info+video endpoint order used by MediaX's youtube-free.js.
        try:
            response = client.get(
                "https://weirddl.sbs/api/info",
                params={"url": url},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Provider metadata was not JSON object")
            download_url = "https://weirddl.sbs/api/download?url=" + quote(url, safe="")
            Path(destination).unlink(missing_ok=True)
            downloaded = _receive(client, download_url, destination, max_bytes)
            return downloaded, "weirddl-stream"
        except Exception as exc:
            errors.append(f"weirddl:{type(exc).__name__}")
            Path(destination).unlink(missing_ok=True)

        for base_url in INVIDIOUS_INSTANCES:
            try:
                response = client.get(
                    f"{base_url}/api/v1/videos/{video_id}",
                    params={"local": "true"},
                )
                response.raise_for_status()
                payload = response.json()
                options = payload.get("formatStreams") or []
                options = [
                    item for item in options
                    if isinstance(item, dict) and _stream_url_ok(str(item.get("url") or ""))
                ]
                # These are progressive/muxed video streams, generally 360p/720p.
                options.sort(key=lambda item: (
                    float(item.get("size") or item.get("clen") or 10**12),
                    int(re.search(r"\d+", str(item.get("qualityLabel") or "9999")).group(0))
                    if re.search(r"\d+", str(item.get("qualityLabel") or "9999")) else 9999,
                ))
                for candidate in options[:2]:
                    try:
                        Path(destination).unlink(missing_ok=True)
                        downloaded = _receive(
                            client, str(candidate["url"]), destination, max_bytes,
                        )
                        return downloaded, "invidious"
                    except (httpx.HTTPError, ValueError, OSError):
                        Path(destination).unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f"invidious:{type(exc).__name__}")

        for base_url in PIPED_INSTANCES:
            try:
                response = client.get(f"{base_url}/streams/{video_id}")
                response.raise_for_status()
                payload = response.json()
                streams = payload.get("videoStreams") or []
                combined = [
                    item for item in streams
                    if isinstance(item, dict)
                    and item.get("videoOnly") is False
                    and _stream_url_ok(str(item.get("url") or ""))
                ]
                combined.sort(key=lambda item: (
                    int(item.get("contentLength") or 10**12),
                    int(item.get("height") or 9999),
                ))
                for candidate in combined[:2]:
                    try:
                        Path(destination).unlink(missing_ok=True)
                        downloaded = _receive(
                            client, str(candidate["url"]), destination, max_bytes,
                        )
                        return downloaded, "piped"
                    except (httpx.HTTPError, ValueError, OSError):
                        Path(destination).unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f"piped:{type(exc).__name__}")

    Path(destination).unlink(missing_ok=True)
    raise RuntimeError("MediaX-like provider video download unavailable: " + ",".join(errors))
