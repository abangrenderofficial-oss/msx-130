"""Lazy, self-hosted bgutil HTTP PO Token server lifecycle for Musix.

No external API keys or account cookies are required to launch the provider.
The provider runs only as a localhost child of the existing Musix bot.
When Musix restarts, the next YouTube request starts it again.
"""
from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.request

HOME_DEFAULT = "/home/container/.musix_pot_provider"
_LOCAL_URL = "http://127.0.0.1:4416"
_lock = threading.Lock()
_child: subprocess.Popen | None = None


def _enabled() -> bool:
    return os.getenv("MUSIX_VIDEO_FIRST_POT_AUTO_START", "").lower() in {
        "1", "true", "yes", "on"
    }


def _healthcheck() -> bool:
    try:
        with urllib.request.urlopen(_LOCAL_URL + "/ping", timeout=0.7) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def _deno_executable() -> pathlib.Path:
    bundled = pathlib.Path("/home/container/bin/deno")
    if bundled.is_file():
        return bundled
    import shutil
    found = shutil.which("deno")
    if found:
        return pathlib.Path(found)
    raise RuntimeError("Bundled Deno executable not installed")


def _stop() -> None:
    global _child
    with _lock:
        if _child is not None and _child.poll() is None:
            _child.terminate()
            try:
                _child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _child.kill()
        _child = None


def ensure_local_provider() -> str | None:
    """Return reachable localhost provider URL, or None if explicitly off.

    When flag is on but the service cannot start, fail closed to caller.
    This doesn't modify cookies or the Bot Hosting startup command.
    """
    global _child
    if not _enabled():
        return None

    with _lock:
        if _healthcheck():
            return _LOCAL_URL

        home = pathlib.Path(os.getenv("MUSIX_POT_HOME", HOME_DEFAULT))
        server = home / "bgutil-2.0.2/server"
        entry = server / "src/main.ts"
        modules = server / "node_modules"
        if not entry.is_file() or not modules.is_dir():
            raise RuntimeError("PO Token server not installed; run setup script")
        if _child is not None:
            if _child.poll() is None:
                _child.terminate()
                try:
                    _child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _child.kill()
            _child = None

        _child = subprocess.Popen(
            [str(_deno_executable()), "run", "--allow-env", "--allow-net",
             "--allow-ffi=.", "--allow-read=.", "../src/main.ts",
             "--port", "4416", "--host", "127.0.0.1"],
            cwd=str(modules),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        for _ in range(30):
            if _child.poll() is not None:
                break
            if _healthcheck():
                return _LOCAL_URL
            time.sleep(0.2)

        _child.terminate() if _child.poll() is None else None
        _child = None
        raise RuntimeError("Local PO Token Provider failed readiness check")


atexit.register(_stop)
