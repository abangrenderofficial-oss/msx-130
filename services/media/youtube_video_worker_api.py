"""Private aiohttp API for independent Musix Video-First downloader nodes.

Run one process per node/deployment, with separate egress and optional
YTDLP_YOUTUBE_COOKIES_FILE. This process does NOT start Telegram polling.
Reverse proxy must terminate TLS when exposed off-machine.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from aiohttp import web

from services.media.youtube_video_first import download_video_first_mp3

_MAX_AUDIO_BYTES = 49 * 1024 * 1024
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def canonical_youtube_url(raw: object) -> str:
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("invalid_url")
    try:
        parts = urlsplit(raw.strip())
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        raise ValueError("invalid_url") from exc
    if parts.scheme != "https" or parts.username or parts.password:
        raise ValueError("invalid_url")
    vid: str | None = None
    if host == "youtu.be":
        vid = parts.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts.path == "/watch":
            vid = parse_qs(parts.query).get("v", [None])[0]
        elif parts.path.startswith(("/shorts/", "/live/")):
            vid = parts.path.strip("/").split("/")[1]
    if not vid or not _VIDEO_ID.fullmatch(vid):
        raise ValueError("invalid_url")
    return f"https://www.youtube.com/watch?v={vid}"


def _token_matches(request: web.Request, secret: str) -> bool:
    presented = request.headers.get("X-Musix-Worker-Token", "")
    return len(presented) > 0 and hmac.compare_digest(presented, secret)


def _classify_failure(exc: Exception) -> str:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        msg = str(exc).lower()
        if any(marker in msg for marker in ("sign in to confirm", "login required", "authentication required", "use --cookies")):
            return "context_unavailable"
        if any(marker in msg for marker in ("geo restricted", "not available in your country")):
            return "context_unavailable"
        if any(marker in msg for marker in ("429", "too many requests")):
            return "upstream_rate_limited"
        exc = exc.__cause__ or exc.__context__
    return "download_failed"


def make_app(*, secret: str | None = None, capacity: int = 2, extractor=None) -> web.Application:
    if secret is None:
        secret = os.getenv("MUSIX_VIDEO_FIRST_WORKER_TOKEN", "")
    if len(secret) < 16:
        raise ValueError("MUSIX_VIDEO_FIRST_WORKER_TOKEN needs at least 16 characters")
    if capacity < 1 or capacity > 8:
        raise ValueError("worker capacity must be 1..8")
    extractor = extractor or download_video_first_mp3
    semaphore = asyncio.Semaphore(capacity)
    active = 0

    @web.middleware
    async def require_auth(request: web.Request, handler):
        if not _token_matches(request, secret):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(_request: web.Request) -> web.Response:
        cookie_file = (os.getenv("YTDLP_YOUTUBE_COOKIES_FILE") or "").strip()
        cookie_exists = False
        if cookie_file:
            cookie_path = Path(cookie_file)
            cookie_exists = cookie_path.is_file() and cookie_path.stat().st_size > 0
        # This is a capability hint, NOT proof the cookie has not expired.
        return web.json_response({
            "ok": True, "active": active, "capacity": capacity,
            "cookie_domains": ["youtube.com"] if cookie_exists else [],
        })

    async def audio(request: web.Request) -> web.StreamResponse:
        nonlocal active
        if request.content_length is not None and request.content_length > 4096:
            return web.json_response({"error": "invalid_request"}, status=413)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError()
            url = canonical_youtube_url(payload.get("url"))
            bitrate = payload.get("bitrate_kbps")
            if type(bitrate) is not int or not 64 <= bitrate <= 320:
                raise ValueError()
        except (ValueError, TypeError, web.HTTPException):
            return web.json_response({"error": "invalid_request"}, status=400)

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return web.json_response({"error": "busy"}, status=503)
        active += 1
        try:
            with tempfile.TemporaryDirectory(prefix="musix-youtube-video-") as work:
                try:
                    audio_file = await extractor(
                        url, str(Path(work) / "source.%(ext)s"), bitrate,
                    )
                    path = Path(audio_file)
                    if not path.is_file() or path.stat().st_size < 1024:
                        raise ValueError("invalid_audio")
                    if path.stat().st_size > _MAX_AUDIO_BYTES:
                        return web.json_response({"error": "audio_too_large"}, status=422)
                except Exception as exc:
                    category = _classify_failure(exc)
                    status = 409 if category == "context_unavailable" else 503 if category == "upstream_rate_limited" else 422
                    return web.json_response({"error": category}, status=status)

                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "audio/mpeg",
                        "Content-Length": str(path.stat().st_size),
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
                await response.prepare(request)
                with path.open("rb") as src:
                    while data := src.read(256 * 1024):
                        await response.write(data)
                await response.write_eof()
                return response
        finally:
            active -= 1
            semaphore.release()

    app = web.Application(middlewares=[require_auth], client_max_size=4096)
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/audio", audio)
    return app


def main() -> None:
    # Defaults to loopback for safe lab use; off-host requires HTTPS ingress
    # and a private strong secret configured on both ends.
    address = os.getenv("MUSIX_VIDEO_FIRST_WORKER_BIND", "127.0.0.1")
    port = int(os.getenv("MUSIX_VIDEO_FIRST_WORKER_PORT", "8099"))
    capacity = int(os.getenv("MUSIX_VIDEO_FIRST_WORKER_CAPACITY", "2"))
    web.run_app(make_app(capacity=capacity), host=address, port=port)


if __name__ == "__main__":
    main()
