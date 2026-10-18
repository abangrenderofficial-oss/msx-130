#!/bin/sh
set -eu
umask 077
test -n "${MUSIX_WORKER_SECRET:-}" || { echo "MUSIX_WORKER_SECRET missing" >&2; exit 1; }
cp -R /usr/share/novnc/. /srv/novnc/
Xvfb :99 -screen 0 1350x900x24 -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!
x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -nopw -quiet >/dev/null 2>&1 &
VNC_PID=$!
websockify --heartbeat=20 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 &
WS_PID=$!
trap 'kill "$WS_PID" "$VNC_PID" "$XVFB_PID" 2>/dev/null || true' EXIT
exec python /srv/session/worker.py
