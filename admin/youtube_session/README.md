# Musix Admin — YouTube Session (Built, NOT deployed)

**Scope:** standalone admin dashboard and isolated browser worker only. No edits to live Musix bot startup, Telegram handlers, existing production Dockerfile, MediaX or PayPing. No Google account connected.

## Implemented

- Responsive dark admin interface with YouTube session status, worker health, Browser control, Verify, Lock, Revoke, Activity Logs.
- Owner-only dashboard login: scrypt password hash plus 6-digit TOTP, session rotation with server-only cookie, HttpOnly / Secure / SameSite=Strict, same-origin CSRF enforcement, login throttling, and no self-registration.
- One-use five-minute browser-control ticket; remote access via authenticated WebSocket gateway, with no direct public VNC or Chrome debugging ports.
- Separate Playwright Chromium worker with a *real* browser and address bar accessed via noVNC remote desktop, not an iframe of Google or a fake Google form.
- Chromium persistent user-data profile on dedicated host-mounted browser vault. Worker status API reports only metadata; no cookies/passwords are returned.
- Verify inspects actual signed-in YouTube UI; cookies on disk alone are never treated as proof. Revocation terminates browser and deletes only worker profile.
- The code intentionally does not yet connect a session to downloader tasks. Owner account is never forwarded to a provider relay, bot database, or third-party downloader.

## Required setup BEFORE any deployment

1. Provide **separate hosting** for the browser worker. It requires graphical Chromium, Xvfb, noVNC and more resources than the existing Telegram bot. Do not deploy these files to current Musix Bot Hosting Python process.
2. Provision a genuinely **encrypted persistent disk/volume**, owned by the worker UID 10002 and with directory mode 0700. Mount this path to /vault using MUSIX_ENCRYPTED_VAULT_DIR. An ordinary Docker named volume or bind mount does not provide encryption by itself.
3. Run locally: python admin/youtube_session/bootstrap_admin.py. Add the generated TOTP seed to your authenticator app and keep the generated .env file private; it is git-ignored. Never upload it to GitHub, Telegram, or ChatGPT.
4. Edit .env to use a REAL HTTPS domain as MUSIX_ADMIN_PUBLIC_ORIGIN and the encrypted host mount. HTTPS reverse proxy MUST support WebSockets to the dashboard localhost port 8765.
5. In an independent staging host, validate config: docker compose -f admin/youtube_session/compose.yaml config. Only when deployment is explicitly authorized: docker compose -f admin/youtube_session/compose.yaml up --build -d.
6. Admin logs in using Musix dashboard passphrase and MFA (not Google password). Click Open Secure Browser to enter the actual Chromium window, navigate Google/YouTube, sign in manually, Verify, and Lock.
7. Restart the isolated worker to test persistent browser profile. Then verify signed-in status again. Login might still be invalid due to Google session expiry or anti-automation blocking.

## Security limitations and deployment gates

- This is **pre-deployment source code** and UI. It has NOT passed a real Google sign-in integration test, deployment hardening review, browser worker container smoke test, or end-to-end YouTube download test.
- Google can reject remote/automated browsers. OAuth Google sign-in is not a substitute for authenticated YouTube media access; persistence cannot ensure sessions remain logged in indefinitely.
- The worker requests Chromium sandbox and runs as non-root with limited privileges. If host kernel blocks user namespaces, startup will fail. Fix the hosting security capabilities instead of silently disabling sandbox.
- Dashboard public origin must be HTTPS. Worker ports 6080 (VNC WebSocket) and 8787 (internal API) must never be published. Google passwords and MFA are entered ONLY on Google URLs visible in the real browser.
- Browser remote desktop infrastructure is highly sensitive. It sees the screen and handles input by necessity. Run it on trusted private infrastructure with TLS, no screen recording, no request-body logging, network and runtime isolation.
- Provide independent encrypted vault / secret manager. Database holds audit event types and timestamps only; no browser data. Never back up profile to unsecured storage.
- Memory-only admin sessions intentionally require another passphrase + TOTP after dashboard restarts. Profile storage is separate from dashboard session storage.
- Implement idle auto-lock, runtime isolation checks, worker download permission scopes, queue limits and security testing before connecting actual user downloads.

## APIs

| Endpoint | Purpose |
|---|---|
| POST /login | Dashboard passphrase + TOTP |
| GET /api/me | Authenticated UI CSRF token |
| GET /api/status | Read sanitized browser state from worker |
| POST /api/browser-ticket | Single-use browser ticket, 300s |
| GET /browser/ws?ticket=... | Authenticated websocket to internal noVNC |
| POST /api/verify | Worker checks current YouTube signed-in UI |
| POST /api/lock | Revokes control tickets and closes remote sessions |
| POST /api/revoke | Confirm with REVOKE; deletes dedicated browser profile |
| GET /api/events | Redacted audit log |
| POST /api/logout | Destroy admin session |

### Non-goals for this branch

Do not automatically download YouTube, copy Google login cookies to Musix, mirror media, or modify any production service. Actual login and persistent-state testing must happen first on an isolated staging environment.
