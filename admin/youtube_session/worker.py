"""Isolated persistent headed Chromium worker. Internal Docker network only.

Human signs in on real Google/YouTube pages in Chromium via noVNC.
Never reads/export cookies or credentials, and never sends profile to the bot.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import shutil
import time
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright

WORKER_TOKEN = os.environ.get("MUSIX_WORKER_SECRET", "")
VAULT = Path(os.environ.get("MUSIX_BROWSER_VAULT", "/vault"))
PROFILE = VAULT / "chromium"
CHROMIUM = os.environ.get("MUSIX_CHROMIUM_BINARY", "/usr/bin/chromium")
CHROME = None
CONTEXT = None
PAGE = None
CURRENT_STATUS = "DISCONNECTED"
CHECKED_AT = None
LOCK = asyncio.Lock()
PLAYWRIGHT = None


async def start_browser() -> None:
    global CHROME, CONTEXT, PAGE, PLAYWRIGHT, CURRENT_STATUS, CHECKED_AT
    PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROFILE.chmod(0o700)
    if PLAYWRIGHT is None:
        PLAYWRIGHT = await async_playwright().start()
    CONTEXT = await PLAYWRIGHT.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        executable_path=CHROMIUM,
        headless=False,
        viewport={"width": 1280, "height": 800},
        args=["--window-size=1350,900", "--no-default-browser-check", "--disable-dev-shm-usage"],
        chromium_sandbox=True,
    )
    CONTEXT.set_default_timeout(15000)
    PAGE = CONTEXT.pages[0] if CONTEXT.pages else await CONTEXT.new_page()
    # Only browse to YouTube; Google sign-in redirects happen inside this browser.
    try:
        await PAGE.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=25000)
    except Exception:
        pass
    CURRENT_STATUS = "UNVERIFIED"
    CHECKED_AT = None


@web.middleware
async def internal_auth(request: web.Request, handler):
    token = request.headers.get("X-Worker-Token", "")
    if not WORKER_TOKEN or not hmac.compare_digest(token, WORKER_TOKEN):
        raise web.HTTPForbidden()
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    return response


async def status(request: web.Request) -> web.Response:
    return web.json_response({
        "status": CURRENT_STATUS,
        "worker": "RUNNING" if CONTEXT and PAGE else "WORKER_OFFLINE",
        "profile_present": PROFILE.exists() and any(PROFILE.iterdir()),
        "last_checked": CHECKED_AT,
        "profile_storage": "persistent-mounted",
        "credential_access": "worker-only",
    })


async def verify(request: web.Request) -> web.Response:
    global CURRENT_STATUS, CHECKED_AT
    async with LOCK:
        if PAGE is None:
            return web.json_response({"status": "WORKER_OFFLINE"})
        # Never infer valid Google auth merely from cookies on disk. Inspect
        # the actual signed-in player UI; remain cautious if it is ambiguous.
        try:
            await PAGE.goto(
                "https://www.youtube.com/feed/you",
                wait_until="domcontentloaded",
                timeout=22000,
            )
            await PAGE.wait_for_timeout(1200)
            signed_in = await PAGE.evaluate("""() => {
                const avatar = document.querySelector(
                    'button#avatar-btn, ytd-topbar-menu-button-renderer button#avatar-btn'
                );
                const signIn = document.querySelector(
                    'ytd-button-renderer a[href*="ServiceLogin"], a[href*="accounts.google.com/ServiceLogin"]'
                );
                return Boolean(avatar && !signIn);
            }""")
            CURRENT_STATUS = "CONNECTED" if signed_in else "RECONNECT_REQUIRED"
        except Exception:
            CURRENT_STATUS = "UNVERIFIED"
        CHECKED_AT = int(time.time())
        return await status(request)


async def revoke(request: web.Request) -> web.Response:
    global CONTEXT, PAGE, CURRENT_STATUS, CHECKED_AT
    async with LOCK:
        if CONTEXT is not None:
            try:
                await CONTEXT.close()
            finally:
                CONTEXT = None
                PAGE = None
        # Remove only the dedicated profile path, never parent vault contents.
        if PROFILE.parent.resolve() != VAULT.resolve() or PROFILE.name != "chromium":
            raise web.HTTPInternalServerError(text="Unsafe profile deletion path")
        shutil.rmtree(PROFILE, ignore_errors=False)
        CURRENT_STATUS = "DISCONNECTED"
        CHECKED_AT = None
        await start_browser()
        # Fresh browser is UNVERIFIED by design, but no old Google profile exists.
        CURRENT_STATUS = "DISCONNECTED"
        return await status(request)


async def on_startup(app: web.Application):
    if len(WORKER_TOKEN) < 32:
        raise RuntimeError("MUSIX_WORKER_SECRET is missing/too short")
    VAULT.mkdir(parents=True, exist_ok=True, mode=0o700)
    VAULT.chmod(0o700)
    await start_browser()


async def on_cleanup(app: web.Application):
    global PLAYWRIGHT, CONTEXT
    if CONTEXT:
        await CONTEXT.close()
        CONTEXT = None
    if PLAYWRIGHT:
        await PLAYWRIGHT.stop()
        PLAYWRIGHT = None


def create_app():
    app = web.Application(middlewares=[internal_auth])
    app.router.add_get("/status", status)
    app.router.add_post("/verify", verify)
    app.router.add_post("/revoke", revoke)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8787, access_log=None)
