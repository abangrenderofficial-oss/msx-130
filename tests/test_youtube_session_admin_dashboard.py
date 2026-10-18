"""Offline admin gateway tests. No Chromium, Google, Telegram or live requests."""
import base64
import hashlib
import hmac
import importlib.util
import struct
from pathlib import Path

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

SRC = Path(__file__).resolve().parents[1] / "admin" / "youtube_session" / "dashboard_server.py"


def load_dashboard(monkeypatch, tmp_path):
    password = "a-secret-admin-passphrase-that-is-very-long"
    salt = b"t" * 24
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    monkeypatch.setenv("MUSIX_ADMIN_PUBLIC_ORIGIN", "http://localhost:8765")
    monkeypatch.setenv("MUSIX_DASHBOARD_DEV_HTTP", "1")
    monkeypatch.setenv("MUSIX_WORKER_SECRET", "w" * 40)
    monkeypatch.setenv("MUSIX_ADMIN_PASSWORD_SCRYPT", salt.hex() + "$" + digest.hex())
    monkeypatch.setenv("MUSIX_ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    spec = importlib.util.spec_from_file_location("dashboard_session_test", SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STORE = tmp_path / "admin_events.sqlite"
    module.NOVNC_ROOT = tmp_path / "novnc"
    module.NOVNC_ROOT.mkdir()
    return module, password


def test_scrypt_hash_not_plain_password_and_totp(monkeypatch, tmp_path):
    m, password = load_dashboard(monkeypatch, tmp_path)
    assert m.password_matches(password)
    assert not m.password_matches("incorrect-password")
    seed = base64.b32decode("JBSWY3DPEHPK3PXP")
    stamp = 1700000000
    counter = stamp // 30
    digest = hmac.new(seed, struct.pack(">Q", counter), hashlib.sha1).digest()
    index = digest[-1] & 0x0F
    expected = str((int.from_bytes(digest[index:index + 4], "big") & 0x7FFFFFFF) % 1000000).zfill(6)
    assert m.totp_matches(expected, now=stamp)
    assert not m.totp_matches("password", now=stamp)


def test_rejects_insecure_public_origin(monkeypatch, tmp_path):
    m, _ = load_dashboard(monkeypatch, tmp_path)
    m.DEV_HTTP = False
    with pytest.raises(RuntimeError, match="HTTPS"):
        m.require_configuration()
    m.DEV_HTTP = True
    m.WORKER_URL = "https://untrusted.external"
    with pytest.raises(RuntimeError, match="private"):
        m.require_configuration()


@pytest.mark.asyncio
async def test_owner_session_csrf_and_browser_ticket(monkeypatch, tmp_path):
    m, pw = load_dashboard(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "totp_matches", lambda code: code == "123456")
    server = TestServer(m.create_app())
    client = TestClient(server, cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        assert (await client.get("/api/me")).status == 401
        bad = await client.post("/login", json={"password": pw, "totp": "123456"})
        assert bad.status == 403  # forged/no Origin

        ok = await client.post(
            "/login", json={"password": pw, "totp": "123456"},
            headers={"Origin": m.ORIGIN},
        )
        assert ok.status == 200
        session = await ok.json()
        assert session["csrf"]
        cookie = ok.cookies[m.COOKIE]
        assert cookie["httponly"]
        assert cookie["samesite"].lower() == "strict"

        me = await client.get("/api/me")
        assert (await me.json())["authenticated"] is True
        assert (await client.post("/api/browser-ticket", json={})).status == 403
        headers = {"Origin": m.ORIGIN, "X-CSRF-Token": session["csrf"]}
        ticket_response = await client.post("/api/browser-ticket", json={}, headers=headers)
        assert ticket_response.status == 200
        ticket = await ticket_response.json()
        assert ticket["ttl_seconds"] == 300
        assert ticket["url"].startswith("/browser?ticket=")
        # Access to admin page is allowed but remote websocket needs a live
        # one-use ticket AND origin; no Google credentials are involved.
        assert (await client.get("/browser")).status == 200
        assert (await client.post("/api/revoke", json={"confirmation": "NO"}, headers=headers)).status == 400
        assert (await client.post("/api/lock", json={}, headers=headers)).status == 200
        assert not m.TICKETS
        assert (await client.post("/api/logout", json={}, headers=headers)).status == 200
        assert (await client.get("/api/me")).status == 401
    finally:
        await client.close()
