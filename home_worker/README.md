# MusiX Home Audio Worker — opt-in, no-cookies experiment

**This branch is not production.** \`HOME_WORKER_ENABLED=false\` is the default.
No YouTube, Telegram or PayPing settings need changing until you choose to test.

## Architecture

1. The existing MusiX bot checks its normal global \`file_id\` cache.
2. The optional home-worker path also checks the old \`downloaded_files\` cache.
3. On a cache miss and a fresh **ready** worker heartbeat, MusiX creates a
   PostgreSQL job in \`home_audio_jobs\` and shows a queue message.
4. The home PC makes OUTBOUND HTTPS requests to the existing MusiX website's
   \`/api/home-worker/{heartbeat,claim,complete}\` endpoints. The worker never
   accepts inbound connections and has NO database credentials.
5. The worker uses yt-dlp's **bestaudio/best** with cookies OFF by default.
   FFmpeg converts to MP3, checks MP3 duration and the 49 MiB file cap.
6. Since Telegram has no generic "upload file without sending" endpoint,
   the worker first sends the MP3 to a PRIVATE staging channel using the
   **same MusiX bot token**, obtains a Telegram \`file_id\`, and reports it.
7. The bot saves the file ID in its existing local and global music caches,
   then sends audio to the end user through the existing music sender so
   captions, playlist and private admin monitor behaviour remain intact.
8. Offline, no heartbeat for 180 seconds, failed download or 90-second
   deadline: original Music downloader paths run unchanged.

**Limits:** The 90-second deadline includes extraction, transcode and staging
upload; long videos may time out, then the old pipeline runs. The worker
must stay online to process work. The bot continues on its own if the PC
is turned off. Do not point staging at the owner audit/monitor group.

## Windows installation (not production deployment)

1. Create a PRIVATE Telegram channel specifically for staging; add the
   existing MusiX bot as admin with permission to post. Obtain channel chat
   ID from Telegram bot API or your preferred admin tool. Never put a bot
   token in browser address bars or public screenshots.
2. Install Python 3.11+ and FFmpeg; confirm \`python --version\`,
   \`ffmpeg -version\` and \`ffprobe -version\` in PowerShell.
3. Clone/check out branch \`feature/home-worker-no-cookies\` on your HOME PC.
   Navigate to \`home_worker\`:
   \`\`\`powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item .env.example .env
   \`\`\`
4. Set \`MUSIX_PUBLIC_URL\`, \`BOT_TOKEN\`, \`STAGING_CHAT_ID\` and
   \`HOME_WORKER_API_TOKEN\` in local \`.env\`; generate a strong token in
   PowerShell using Python \`secrets.token_urlsafe(48)\`.
5. In a SEPARATE staging MusiX deployment (not original production), run
   the Alembic migration \`20261020_000020\` on the staging PostgreSQL database.
   Set bot-side \`HOME_WORKER_API_TOKEN\` to the same token. Keep
   \`HOME_WORKER_ENABLED=false\` until all staging checks pass; then
   explicitly switch to \`true\`.
6. Run \`.\.venv\Scripts\python.exe worker.py\` in PowerShell. The worker
   reports only video IDs, timings and error categories, never auth tokens.
7. For Windows Task Scheduler: Create Task > General > "Run whether user is
   logged on or not"; Trigger "At startup"; Action "Start a program":
   \`C:\path\to\home_worker\.venv\Scripts\python.exe\`; argument
   \`C:\path\to\home_worker\worker.py\`; Start in
   \`C:\path\to\home_worker\`. In Settings enable restart after failure.
   Disable Windows sleep while running the worker. If the PC is OFF,
   the worker is offline and the bot automatically uses its old pipeline.

**Staging checklist before real traffic:** no port forwarding, persistent
PostgreSQL, secure HTTPS public website, private staging chat, valid
\`BOT_TOKEN\` access, strong shared API token, and no credentials in GitHub.

## Rate limits and recovery

\`HOME_MIN_YOUTUBE_DELAY_SECONDS\` (8), \`HOME_MAX_JOBS_PER_HOUR\` (30),
`HOME_MAX_CONCURRENT_JOBS` (1 by default, 1–4 per process), no cookies by default.
Use **one** worker process on the same home IP so the delay and hourly limit
remain global to that PC.
After a YouTube bot-check/429 the worker reports the error, enters a
configurable cooldown and heartbeats \`ready=false\`, so new user requests
skip the home queue during cooldown. Jobs are claimed with PostgreSQL
\`SELECT .. FOR UPDATE SKIP LOCKED\` and a random fencing token to prevent
stale worker completion; expired leases requeue only before the
original bot-side deadline.

Admin: send \`/workerstats\` from owner account to see success rate for
the last 24h/7d and last heartbeat. All telemetry is in
\`home_audio_jobs\` (times, status, video ID and error category) and
\`home_audio_heartbeats\`.

## Test matrix

- **Flag OFF:** no new queue calls, original YouTube worker/cache stays.
- **Cache hit:** global and old local file IDs bypass home-worker job.
- **New URL:** worker is ready, job is picked up, MP3 sent to staging,
  main music handler delivers file ID and stores existing cache.
- **Home PC offline:** after 3 minutes from last heartbeat, original
  downloader is used directly.
- **Timeout/failure:** queue records failure, bot resumes original path.
- **Bot check / 429:** error reported, worker cools down and publishes
  \`ready=false\`; requests during cooldown use original path.
- **Restart and duplicate processes:** locked claiming prevents multiple
  workers taking the same pending job; late results rejected.

Do not claim success until a real MP3 was delivered and played in Telegram.
Do not run tests using content you do not have permission to download.
