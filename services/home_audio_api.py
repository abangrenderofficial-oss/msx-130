"""Minimal authenticated pull API for optional Windows/Linux home audio worker.

No public worker ingress. The home worker initiates every HTTPS request to
the existing Musix admin web port. Never return secrets or auth tokens.
"""
import hmac
import logging
import os

from aiohttp import web

from services import home_audio_queue as queue


def install_routes(app: web.Application, db) -> None:
    if not queue.configured():
        return
    secret = os.getenv("HOME_WORKER_API_TOKEN", "")
    if len(secret) < 32:
        return

    @web.middleware
    async def enforce_authorization(request, handler):
        if request.path.startswith("/api/home-worker/"):
            header = request.headers.get("Authorization", "")
            if not hmac.compare_digest(header, "Bearer " + secret):
                raise web.HTTPUnauthorized()
            if not queue.enabled():
                raise web.HTTPServiceUnavailable()
        return await handler(request)

    app.middlewares.append(enforce_authorization)
    # Do not allow arbitrary HTML/URL ingestion, only small JSON payloads.
    app._client_max_size = min(app._client_max_size, 8192)

    async def payload(request):
        try:
            value = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid JSON")
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="Invalid body")
        return value

    async def heartbeat(request):
        data = await payload(request)
        try:
            await queue.record_heartbeat(db, str(data.get("worker_id", "")), ready=(data.get("ready") is True))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid worker")
        return web.json_response({"ok": True})

    async def claim(request):
        data = await payload(request)
        try:
            job = await queue.claim(db, str(data.get("worker_id", "")))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid worker")
        return web.json_response({"ok": True, "job": job})

    async def complete(request):
        data = await payload(request)
        if not isinstance(data.get("success"), bool):
            raise web.HTTPBadRequest(text="Invalid success")
        record = await queue.complete(
            db,
            worker_id=str(data.get("worker_id", "")),
            job_id=str(data.get("job_id", "")),
            claim_token=str(data.get("claim_token", "")),
            success=data["success"],
            file_id=str(data.get("file_id", "")),
            title=str(data.get("title", "")),
            performer=str(data.get("performer", "")),
            duration=data.get("duration"),
            failure_reason=str(data.get("failure_reason", "")),
        )
        if record is None:
            raise web.HTTPConflict(text="Job expired, missing, or lease no longer valid")
        if record["status"] == "done":
            # Bot-side cache writing means the home PC never gets DB credentials.
            # The original Telegram sender still handles caption/playlist/monitor.
            from services.media.music_download import build_music_cache_key
            from services.storage.music_cache import store_cached_audio
            try:
                await db.add_file(build_music_cache_key(record["url"]), record["file_id"], "audio")
                await store_cached_audio(
                    record["url"],
                    telegram_file_id=record["file_id"],
                    variant="mp3_320",
                    title=record["title"],
                    performer=record["performer"],
                    duration_seconds=record["duration"],
                )
            except Exception:
                logging.exception("Home worker file_id cache write failed")
        return web.json_response({"ok": True})

    app.router.add_post("/api/home-worker/heartbeat", heartbeat)
    app.router.add_post("/api/home-worker/claim", claim)
    app.router.add_post("/api/home-worker/complete", complete)
