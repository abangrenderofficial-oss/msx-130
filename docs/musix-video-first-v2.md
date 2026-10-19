# Musix V2 — Video-First, Audio-Only Delivery (staging)

Updated: 2026-10-19

## Current status

**Experimental / disabled by default. DO NOT enable in production yet.**

- Python implementation based on the *architecture* (not copied source code) of
  [Desiders/ytdl_tg_bot](https://github.com/Desiders/ytdl_tg_bot):
  yt-dlp video acquisition, optional bgutil PO-token provider, independent
  worker-style processing, and local FFmpeg conversion.
- Existing Telegram bot / cache / payment / other music platforms untouched.
- Tested local synthetic video => playable MP3 with audio verification and
  cleanup. Regression tests pass.
- **Real YouTube test, success:** video ID dQw4w9WgXcQ downloaded through the
  new video-first pipeline in the isolated Bot Hosting lab on 2026-10-19.
  Output: 3,409,848-byte MP3, 213.08s, stereo, 44.1kHz, 128kbps;
  18.94s elapsed. FFprobe validation, full FFmpeg decode and video cleanup
  passed. Telegram delivery was not performed.
- **Real YouTube test, failure:** video ID x-QghL7ntSg on 2026-10-19
  remained blocked by YouTube \`Sign in to confirm you're not a bot\` even
  though the self-hosted PO-token provider's health check passed.
  Thus partial success is proven, but broad reliability is NOT proven.
- Do not conflate a healthy PO-token provider with successful YouTube access.
  Authentication / IP challenges may require changes to the deployment's
  allowed access path. Rotating or exporting account cookies manually is
  intentionally not required by this implementation, but cookie-free success
  cannot be promised.

## User-facing flow

1. User shares YouTube URL with Musix.
2. Bot checks cached Telegram audio file_id (unchanged).
3. When flag is enabled, Video-First downloader obtains video+audio:
   prefer combined format with the best known audio bitrate at <= 360p,
   otherwise merge small video with best independent audio.
4. Reject missing/corrupt/silent video using FFprobe.
5. Extract MP3 with FFmpeg; validate output audio track and remove downloaded
   video in all cases, including exceptions.
6. Return MP3 through Musix's existing Telegram delivery pipeline, which
   handles metadata, size caps, multi-part files, file_id caching and cleanup.
7. If Video-First acquisition fails, fall back to the legacy worker/relays
   without changing any other platform handlers.

## Configuration

All flags optional. Nothing changes unless explicitly enabled:

- \`MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED=1\` — opt in to Video-First branch.
- \`MUSIX_VIDEO_FIRST_MAX_MB=350\` — cap per downloaded video source,
  clamped to 10–1024MB; existing audio caps remain enforced.
- \`MUSIX_VIDEO_FIRST_POT_PROVIDER_URL=http://127.0.0.1:4416\` —
  optional server endpoint of a **self-hosted** bgutil HTTP PO-token provider.
  Configures yt-dlp's \`youtubepot-bgutilhttp\` extractor args for *both*
  video metadata and download stages, forcing \`mweb\` with automatic token
  fetching. No URL is auto-started or assumed to exist.

This configuration requires a separately started
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
service and its yt-dlp plugin on the same worker network, plus Node/Deno
and FFmpeg/FFprobe. Bot Hosting runtime may require a separate deployment to
run the Node service. Do not expose the provider publicly without suitable
access controls. The Desiders deployment Dockerfiles were inspected, but
their source is **not** vendored or transplanted into Musix.

**NOTE:** Auth challenges from YouTube can still occur after PO tokens are
generated. Treat \`Sign in\` as an upstream source failure, not an MP3 bug.

## Tests (no YouTube account needed)

\`\`\`sh
pytest -q tests/test_music_download.py tests/test_youtube_video_first.py
\`\`\`

Check lab status before enabling the flag on live Musix:

1. Bring up a private PO-token service in the isolated lab.
2. Probe multiple actual URLs with the exact same egress and credentials as
   the eventual downloader.
3. Record % success, error categories, duration, source bytes, validation.
4. Verify at least one REAL YouTube video file has BOTH audio and video,
   and its MP3 plays end-to-end with FFprobe/FFmpeg. (First pilot: PASS)
5. Test sending MP3 to a staging Telegram chat, not the production bot.
6. Gate rollout on empirical success. No production deployment until then.

## Multi-node worker routing (new)

This is independently implemented in Python, inspired by Desiders'
\`NodeRouter\` and cookie-aware worker selection. The bot does **not**
automatically borrow access or credentials from the public @yv2t_bot.

**Working lab results (2026-10-19):**
- 47/47 routing and music tests passed at the first multi-node stage.
- Actual HTTP connection test: first worker returned an upstream auth
  challenge, second worker delivered verified MP3; failover passed.
- Actual YouTube link \`dQw4w9WgXcQ\` through a **real local HTTP worker**,
  bgutil provider and Musix router returned valid 3,409,848-byte MP3 in
  **19.07 seconds**. No Telegram production delivery tested.
- The sample \`x-QghL7ntSg\` is still known to trigger a YouTube account/IP
  challenge on the lab node. Additional nodes **only help if they have
  genuinely independent, usable access contexts**.
- None of these tests proves production success rates.

### Worker deployment

Run the code on EACH isolated worker deployment with a different private
network/server context when possible:

\`\`\`shell
# On a WORKER deployment (never commit any of these secrets)
MUSIX_VIDEO_FIRST_WORKER_TOKEN=<strong-random-secret-at-least-16-chars>
MUSIX_VIDEO_FIRST_WORKER_BIND=127.0.0.1
MUSIX_VIDEO_FIRST_WORKER_PORT=8099
MUSIX_VIDEO_FIRST_WORKER_CAPACITY=2

# Optional, worker-local YouTube access settings
MUSIX_VIDEO_FIRST_POT_PROVIDER_URL=http://127.0.0.1:4416
YTDLP_YOUTUBE_COOKIES_FILE=/path/to/worker-secret/youtube.txt

python -m services.media.youtube_video_worker_api
\`\`\`

Set only worker deployment secrets, not any user's Telegram bot token. This
repository's existing \`services/__init__.py\` currently imports config on
worker startup; for a standalone worker without full bot env, the Python
process currently requires a harmless placeholder \`BOT_TOKEN\` and
\`ADMIN_ID=1\` at import time. **Do not copy the production Telegram bot
token to a downloader worker.** Packaging into a no-bot-settings standalone
worker is a release requirement before a public production rollout.

The worker API uses \`X-Musix-Worker-Token\` for \`/v1/health\` and
\`/v1/audio\`. Default bind is loopback. Publish it only through trusted
HTTPS ingress or on a trusted service network with equivalent protection.
Its \`/v1/audio\` response contains MP3, **not raw YouTube video**.

### Configure bot-side routing

In the Musix **bot** deployment (once production tests pass):

\`\`\`shell
MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED=1
MUSIX_WORKER_A_TOKEN=<same-secret-as-worker-A>
MUSIX_WORKER_B_TOKEN=<same-secret-as-worker-B>
MUSIX_VIDEO_FIRST_NODES_JSON='[
  {"name":"worker_a","url":"https://worker-a.example","token_env":"MUSIX_WORKER_A_TOKEN"},
  {"name":"worker_b","url":"https://worker-b.example","token_env":"MUSIX_WORKER_B_TOKEN"}
]'
\`\`\`

These URLs are illustrative placeholders; never deploy with example URLs.
HTTP is allowed only for loopback tests; no tokens inline inside the JSON.
Config errors are handled as failures and the bot's legacy extraction
fallback remains available.

### What the router does

1. Calls authorized \`/v1/health\` on configured nodes.
2. Prefers an available node reporting a YouTube cookie *capability*
   (not a guarantee of valid cookies), then lower active/capacity ratio.
3. Requests video-first extraction on the chosen worker. The worker
   downloads the video locally, validates its audio/video tracks with
   FFprobe, converts it to MP3 with FFmpeg, and deletes video.
4. Streams only MP3 bytes to Musix; Musix checks size, codec, duration
   before its normal Telegram delivery pipeline.
5. On upstream auth/context failure, busy or unavailable worker,
   discards partial media and tries another healthy node. Worker-token
   authentication failure is terminal, not silently retried.
6. If no worker can serve the request, preserves the existing fallback.

### Cookie management: intentionally cautious

Cookies (when required) are provisioned as secrets **on each worker**, never
sent from the bot and never committed into GitHub. Set
\`YTDLP_YOUTUBE_COOKIES_FILE\` per deployment and use a protected mounted
file. On the next request, yt-dlp reads the local file again, so an operator
can update the secret file without editing Musix code. The node's health
endpoint reports only whether the file exists.

**This is NOT an automatic YouTube cookie creator, renewer or distributor.**
It avoids credential copying between services but cannot remove a YouTube
authentication challenge by itself. The Desiders repository also requires
externally supplied cookies; its controller distributes, not renews them.

### Launch requirements

- Re-run the test suite; run actual downloads on **different hosting
  egress contexts**, not simply two processes on one server/IP.
- Verify different challenging videos, especially \`x-QghL7ntSg\`.
- Confirm an actual staging Telegram MP3 send with metadata and cache.
- Keep the PR in draft until those checks pass.
- Bot live remains untouched until explicit production rollout.

## Rollback

Keep \`MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED\` unset or set to \`0\`; the
pre-existing YouTube logic remains selected. The branch is not the live
Bot Hosting deployment (auto-pull on live Musix is currently disabled).
