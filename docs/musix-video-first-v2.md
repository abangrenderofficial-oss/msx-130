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

## Rollback

Keep \`MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED\` unset or set to \`0\`; the
pre-existing YouTube logic remains selected. The branch is not the live
Bot Hosting deployment (auto-pull on live Musix is currently disabled).
