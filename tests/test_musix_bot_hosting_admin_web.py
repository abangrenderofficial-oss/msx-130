"""Owner-issued Telegram login and same-deployment web integration (offline)."""
import importlib.util
from pathlib import Path

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "admin" / "youtube_session" / "dashboard_server.py"


def build(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_DASHBOARD_TELEGRAM_AUTH", "1")
    monkeypatch.setenv("MUSIX_ADMIN_PUBLIC_ORIGIN", "http://localhost:8765")
    monkeypatch.setenv("MUSIX_DASHBOARD_DEV_HTTP", "1")
    monkeypatch.delenv("MUSIX_WORKER_SECRET", raising=False)
    spec = importlib.util.spec_from_file_location("musix_owner_dashboard_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STORE = tmp_path / "audit.sqlite"
    mod.NOVNC_ROOT = tmp_path / "novnc_not_installed"
    return mod


@pytest.mark.asyncio
async def test_same_deployment_owner_login_worker_locked(monkeypatch, tmp_path):
    from config import ADMINS_UID

    mod = build(monkeypatch, tmp_path)
    app = mod.create_app()
    server = TestServer(app)
    client = TestClient(server, cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    origin = {"Origin": "http://localhost:8765"}
    try:
        public = await client.get("/health")
        assert public.status == 200
        assert (await public.json())["ok"] is True
        page = await client.get("/admin/youtube-session")
        assert page.status == 200
        assert "/sessionlogin" in await page.text()
        assert (await client.get("/api/status")).status == 401

        owner = int(next(iter(ADMINS_UID)))
        code = mod.issue_telegram_login_code(owner)
        assert code
        assert (await client.post("/login", headers=origin, json={"code": "wrong"})).status == 401
        ok = await client.post("/login", headers=origin, json={"code": code})
        assert ok.status == 200
        csrf = (await ok.json())["csrf"]
        assert (await client.post("/login", headers=origin, json={"code": code})).status == 401
        state = await client.get("/api/status")
        blocked = await state.json()
        assert blocked["status"] == "WORKER_NOT_CONFIGURED"
        assert blocked["worker"] == "WORKER_OFFLINE"
        assert blocked["can_open_browser"] is False
        assert blocked["blocker"] == "CHROMIUM_SANDBOX_UNAVAILABLE"
        secured = {**origin, "X-CSRF-Token": csrf}
        response = await client.post("/api/browser-ticket", headers=secured, json={})
        assert response.status == 503
        assert (await client.get("/api/me")).status == 200
        assert (await client.post("/api/logout", headers=secured, json={})).status == 200
        assert (await client.get("/api/me")).status == 401
    finally:
        await client.close()


def test_non_owner_cannot_generate_login_code(monkeypatch, tmp_path):
    from config import ADMINS_UID

    mod = build(monkeypatch, tmp_path)
    outsider = max(int(uid) for uid in ADMINS_UID) + 12345
    with pytest.raises(PermissionError):
        mod.issue_telegram_login_code(outsider)
