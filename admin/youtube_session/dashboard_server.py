"""Owner-only Musix YouTube browser dashboard. Standalone; does not start Telegram.

Admin passphrase + TOTP authenticate the dashboard. Google credentials never
enter this service. Remote browser pixels/input pass through an authenticated
WebSocket to an isolated worker, without capture/logging. No Google cookie APIs.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import struct
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientSession, WSMsgType, web

BASE = Path(__file__).resolve().parent
BOT_HOSTING_MODE = os.getenv("MUSIX_DASHBOARD_TELEGRAM_AUTH") == "1"
LOGIN_CODES: dict[str, float | str] = {}
LOGIN_CODE_SECRET = secrets.token_bytes(32)
ORIGIN = os.getenv("MUSIX_ADMIN_PUBLIC_ORIGIN", "").rstrip("/")
WORKER_URL = os.getenv("MUSIX_WORKER_URL", "http://browser-worker:8787").rstrip("/")
WORKER_SOCKET = os.getenv("MUSIX_WORKER_VNC_WS", "ws://browser-worker:6080")
WORKER_SECRET = os.getenv("MUSIX_WORKER_SECRET", "")
PASSWORD_HASH = os.getenv("MUSIX_ADMIN_PASSWORD_SCRYPT", "")
TOTP_SECRET = os.getenv("MUSIX_ADMIN_TOTP_SECRET", "")
DEV_HTTP = os.getenv("MUSIX_DASHBOARD_DEV_HTTP") == "1"
STORE = Path(os.getenv("MUSIX_ADMIN_AUDIT_DB", "/state/admin_audit.sqlite"))
NOVNC_ROOT = Path(os.getenv("MUSIX_NOVNC_STATIC", "/usr/share/novnc"))
SESSION_LIFETIME = 8 * 3600
TICKET_LIFETIME = 300
CONTROL_LIFETIME = 10 * 60
COOKIE = "musix_admin_session"
SESSIONS: dict[str, dict] = {}
TICKETS: dict[str, dict] = {}
SOCKETS: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
FAILED: dict[str, deque[float]] = defaultdict(deque)


def require_configuration() -> None:
    if not ORIGIN or (not DEV_HTTP and not ORIGIN.startswith("https://")):
        raise RuntimeError("MUSIX_ADMIN_PUBLIC_ORIGIN must be HTTPS")
    parsed = urlparse(ORIGIN)
    if not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Invalid admin public origin")
    if DEV_HTTP and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Development HTTP allowed only on localhost")
    if not BOT_HOSTING_MODE and (len(WORKER_SECRET) < 32 or ":" not in PASSWORD_HASH or not TOTP_SECRET):
        raise RuntimeError("Worker secret, scrypt passphrase hash, and TOTP required")
    if BOT_HOSTING_MODE and not WORKER_SECRET:
        return  # Fail closed: Google browser worker unavailable on this host.
    if not (WORKER_URL.startswith("http://browser-worker:") or WORKER_URL.startswith("http://musix-browser-worker.railway.internal:")):
        raise RuntimeError("Worker API must be private to the Docker network")
    if not (WORKER_SOCKET.startswith("ws://browser-worker:") or WORKER_SOCKET.startswith("ws://musix-browser-worker.railway.internal:")):
        raise RuntimeError("Worker VNC must be private to the Docker network")


def password_matches(password: str) -> bool:
    try:
        salt_hex, hash_hex = PASSWORD_HASH.split(":", 1)
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=2**14, r=8, p=1, dklen=len(expected),
        )
        return hmac.compare_digest(derived, expected)
    except (ValueError, TypeError):
        return False


def totp_matches(code: str, *, now: int | None = None) -> bool:
    import base64
    if not re.fullmatch(r"\d{6}", code):
        return False
    try:
        raw = base64.b32decode(TOTP_SECRET.upper().replace(" ", "") + "=" * (-len(TOTP_SECRET) % 8))
    except (ValueError, base64.binascii.Error):
        return False
    step = int((now or time.time()) // 30)
    for delta in (-1, 0, 1):
        digest = hmac.new(raw, struct.pack(">Q", step + delta), hashlib.sha1).digest()
        index = digest[-1] & 0x0F
        dynamic = int.from_bytes(digest[index:index + 4], "big") & 0x7FFFFFFF
        if hmac.compare_digest(f"{dynamic % 1_000_000:06d}", code):
            return True
    return False


def initialize_audit() -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(STORE) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, time_utc INTEGER NOT NULL,
            action TEXT NOT NULL, outcome TEXT NOT NULL)""")
        conn.commit()


def audit(action: str, outcome: str) -> None:
    # Allowlist events only: never URLs, input text, tokens, account identifiers, cookies.
    if action not in {
        "LOGIN", "LOGIN_DENIED", "TICKET", "BROWSER_OPEN", "BROWSER_LOCK",
        "SESSION_VERIFY", "PROFILE_REVOKE", "WORKER_ERROR", "LOGOUT"
    }:
        action = "WORKER_ERROR"
    with sqlite3.connect(STORE) as conn:
        conn.execute(
            "INSERT INTO events (time_utc, action, outcome) VALUES (?,?,?)",
            (int(time.time()), action, "ok" if outcome == "ok" else "failed"),
        )


def get_session(request: web.Request) -> tuple[str, dict] | None:
    sid = request.cookies.get(COOKIE, "")
    item = SESSIONS.get(sid)
    if not item or item["expiry"] <= time.time():
        SESSIONS.pop(sid, None)
        return None
    return sid, item


def check_origin(request: web.Request) -> bool:
    return request.headers.get("Origin", "") == ORIGIN


@web.middleware
async def security(request: web.Request, handler):
    if request.path.startswith("/api/") or request.path.startswith("/browser"):
        if not get_session(request):
            raise web.HTTPUnauthorized(text="Admin sign-in required")
        if request.method not in {"GET", "HEAD"}:
            if not check_origin(request):
                raise web.HTTPForbidden(text="Origin verification failed")
            item = get_session(request)
            if not item or not hmac.compare_digest(
                request.headers.get("X-CSRF-Token", ""), item[1]["csrf"]
            ):
                raise web.HTTPForbidden(text="CSRF verification failed")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self' wss: ws:; "
        "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


def issue_telegram_login_code(owner_id: int) -> str:
    """Issue single-use passcode only in owner Musix private Telegram chat."""
    from config import ADMINS_UID
    if not BOT_HOSTING_MODE or int(owner_id) not in ADMINS_UID:
        raise PermissionError("Owner-only session login")
    code = secrets.token_urlsafe(16)
    LOGIN_CODES.clear()
    LOGIN_CODES["digest"] = hmac.new(LOGIN_CODE_SECRET, code.encode(), hashlib.sha256).hexdigest()
    LOGIN_CODES["expires"] = time.time() + 180
    return code


def consume_telegram_login_code(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 100:
        return False
    digest = str(LOGIN_CODES.get("digest", ""))
    expiry = float(LOGIN_CODES.get("expires", 0))
    valid = bool(digest and expiry >= time.time() and hmac.compare_digest(
        hmac.new(LOGIN_CODE_SECRET, value.encode(), hashlib.sha256).hexdigest(), digest
    ))
    if valid:
        LOGIN_CODES.clear()
    return valid


async def post_login(request: web.Request):
    ip = request.remote or "unknown"
    recent = FAILED[ip]
    now = time.time()
    while recent and recent[0] < now - 900:
        recent.popleft()
    if len(recent) >= 5:
        raise web.HTTPTooManyRequests(text="Too many attempts")
    if not check_origin(request):
        raise web.HTTPForbidden(text="Bad origin")
    data = await request.json()
    accepted = (consume_telegram_login_code(str(data.get("code", ""))) if BOT_HOSTING_MODE
                else (password_matches(str(data.get("password", ""))) and
                      totp_matches(str(data.get("totp", "")))))
    if accepted:
        sid = secrets.token_urlsafe(40)
        SESSIONS[sid] = {"expiry": now + SESSION_LIFETIME, "csrf": secrets.token_urlsafe(32)}
        audit("LOGIN", "ok")
        response = web.json_response({"ok": True, "csrf": SESSIONS[sid]["csrf"]})
        response.set_cookie(
            COOKIE, sid, httponly=True, secure=not DEV_HTTP,
            samesite="Strict", max_age=SESSION_LIFETIME, path="/",
        )
        return response
    recent.append(now)
    audit("LOGIN_DENIED", "failed")
    raise web.HTTPUnauthorized(text="Credentials not accepted")


async def api_me(request: web.Request):
    item = get_session(request)
    return web.json_response({"authenticated": bool(item), "csrf": item[1]["csrf"] if item else ""})


async def worker_rpc(method: str, path: str) -> dict:
    if BOT_HOSTING_MODE and not WORKER_SECRET:
        # Do not pretend Chromium is installed, running or connected.
        # The existing Bot Hosting runtime blocks unprivileged namespaces;
        # a persistent signed-in Google profile has NOT been provisioned.
        return {
            "status": "WORKER_NOT_CONFIGURED",
            "worker": "WORKER_OFFLINE",
            "profile_present": False,
            "can_open_browser": False,
            "blocker": "CHROMIUM_SANDBOX_UNAVAILABLE",
            "next_step": "Provision an isolated browser runtime with a working sandbox before Google login.",
        }
    async with ClientSession() as client:
        try:
            async with client.request(
                method, f"{WORKER_URL}{path}",
                headers={"X-Worker-Token": WORKER_SECRET},
                timeout=25,
            ) as response:
                response.raise_for_status()
                return await response.json()
        except Exception:
            audit("WORKER_ERROR", "failed")
            return {"status": "WORKER_OFFLINE", "error": "Worker unavailable"}


async def api_status(request: web.Request):
    return web.json_response(await worker_rpc("GET", "/status"))


async def api_events(request: web.Request):
    with sqlite3.connect(STORE) as conn:
        rows = conn.execute(
            "SELECT time_utc,action,outcome FROM events ORDER BY id DESC LIMIT 70"
        ).fetchall()
    return web.json_response({
        "events": [{"time": t, "action": a, "outcome": o} for t, a, o in rows]
    })


async def api_ticket(request: web.Request):
    if BOT_HOSTING_MODE and not WORKER_SECRET:
        raise web.HTTPServiceUnavailable(text="Secure browser worker not configured")
    sid, _ = get_session(request)
    # One unconsumed ticket per session. Tokens are single-use.
    for token, item in list(TICKETS.items()):
        if item["sid"] == sid or item["expires"] < time.time():
            TICKETS.pop(token, None)
    token = secrets.token_urlsafe(36)
    TICKETS[token] = {"sid": sid, "expires": time.time() + TICKET_LIFETIME}
    audit("TICKET", "ok")
    return web.json_response({"ticket": token, "url": "/browser", "ttl_seconds": TICKET_LIFETIME})


async def lock_browser(sid: str):
    for token, item in list(TICKETS.items()):
        if item["sid"] == sid:
            TICKETS.pop(token, None)
    for ws in tuple(SOCKETS.get(sid, set())):
        await ws.close(code=1000, message=b"Browser locked")
    SOCKETS.pop(sid, None)


async def api_lock(request: web.Request):
    sid, _ = get_session(request)
    await lock_browser(sid)
    audit("BROWSER_LOCK", "ok")
    return web.json_response({"ok": True})


async def api_verify(request: web.Request):
    result = await worker_rpc("POST", "/verify")
    audit("SESSION_VERIFY", "ok" if result.get("status") == "CONNECTED" else "failed")
    return web.json_response(result)


async def api_revoke(request: web.Request):
    data = await request.json()
    if data.get("confirmation") != "REVOKE":
        raise web.HTTPBadRequest(text="Type REVOKE to confirm")
    sid, _ = get_session(request)
    await lock_browser(sid)
    result = await worker_rpc("POST", "/revoke")
    audit("PROFILE_REVOKE", "ok" if result.get("status") == "DISCONNECTED" else "failed")
    return web.json_response(result)


async def api_logout(request: web.Request):
    sid, _ = get_session(request)
    await lock_browser(sid)
    SESSIONS.pop(sid, None)
    audit("LOGOUT", "ok")
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


async def browser_websocket(request: web.Request):
    if BOT_HOSTING_MODE and not WORKER_SECRET:
        raise web.HTTPServiceUnavailable(text="Secure browser worker not configured")
    if not check_origin(request):
        raise web.HTTPForbidden(text="Invalid WebSocket origin")
    sid, _ = get_session(request)
    token = request.query.get("ticket", "")
    item = TICKETS.pop(token, None)
    if not item or item["sid"] != sid or item["expires"] < time.time():
        raise web.HTTPForbidden(text="Ticket invalid or used")
    await lock_browser(sid)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    SOCKETS[sid].add(ws)
    audit("BROWSER_OPEN", "ok")
    async def hard_timeout():
        await asyncio.sleep(CONTROL_LIFETIME)
        await ws.close(code=1000, message=b"Browser control TTL expired")
    expiry_task = asyncio.create_task(hard_timeout())
    try:
        async with ClientSession() as client:
            async with client.ws_connect(WORKER_SOCKET, heartbeat=20) as upstream:
                async def forward_down():
                    async for event in upstream:
                        if event.type == WSMsgType.BINARY:
                            await ws.send_bytes(event.data)
                        elif event.type == WSMsgType.TEXT:
                            await ws.send_str(event.data)
                        else:
                            break
                task = asyncio.create_task(forward_down())
                end = time.monotonic() + CONTROL_LIFETIME
                try:
                    async for event in ws:
                        if time.monotonic() >= end:
                            break
                        if event.type == WSMsgType.BINARY:
                            await upstream.send_bytes(event.data)
                        elif event.type == WSMsgType.TEXT:
                            await upstream.send_str(event.data)
                        else:
                            break
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    finally:
        expiry_task.cancel()
        await asyncio.gather(expiry_task, return_exceptions=True)
        SOCKETS[sid].discard(ws)
        await ws.close()
    return ws


def create_app() -> web.Application:
    require_configuration()
    initialize_audit()
    app = web.Application(middlewares=[security], client_max_size=16 * 1024)
    async def health(_request):
        return web.json_response({"ok": True, "service": "musix", "web": "admin", "telegram": "polling"})
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", health)
    app.router.add_get("/admin/youtube-session", lambda r: web.FileResponse(BASE / "dashboard.html"))
    app.router.add_get("/admin", lambda r: web.FileResponse(BASE / "dashboard.html"))
    app.router.add_get("/admin/login", lambda r: web.FileResponse(BASE / "dashboard.html"))
    app.router.add_get("/browser", lambda r: web.FileResponse(BASE / "browser.html"))
    app.router.add_static("/static", BASE / "static", follow_symlinks=False)
    if NOVNC_ROOT.is_dir():
        app.router.add_static("/novnc", NOVNC_ROOT, follow_symlinks=False)
    app.router.add_post("/login", post_login)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/events", api_events)
    app.router.add_post("/api/browser-ticket", api_ticket)
    app.router.add_post("/api/lock", api_lock)
    app.router.add_post("/api/verify", api_verify)
    app.router.add_post("/api/revoke", api_revoke)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/browser/ws", browser_websocket)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8765")), access_log=None)
