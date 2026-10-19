"""Install pinned, open-source YouTube PO-token server (no credentials).

Run explicitly ONCE on Bot Hosting, then it remains on the persistent volume:
    python3 scripts/setup_youtube_pot_provider.py
Re-run safely after image or volume maintenance. This does not restart Musix.

Only installs the provider itself. The bot starts it lazily and binds it to
127.0.0.1, never a public server. Uses pinned upstream tag + commit to avoid
silently changing third-party code on each deployment.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

VERSION = "2.0.2"
PINNED_COMMIT = "26475e9d3665b972c4aeb83b4dbaafcf64b88438"
SOURCE = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
DEFAULT_HOME = "/home/container/.musix_pot_provider"


def _node_tools() -> pathlib.Path:
    candidates = []
    for prefix in (
        pathlib.Path("/home/container/.local"),
        pathlib.Path(sys.prefix),
    ):
        candidates += list(prefix.glob("lib/python*/site-packages/nodejs_wheel/bin"))
    for path in candidates:
        if (path / "node").is_file() and (path / "npm").is_file():
            return path
    raise RuntimeError("Bundled nodejs-wheel bin directory missing")


def _run(command: list[str], *, cwd: pathlib.Path | None = None, env=None) -> None:
    subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=True,
        timeout=180,
        stdout=subprocess.DEVNULL,
    )


def install() -> pathlib.Path:
    home = pathlib.Path(os.getenv("MUSIX_POT_HOME", DEFAULT_HOME)).resolve()
    home.mkdir(parents=True, exist_ok=True)
    final = home / f"bgutil-{VERSION}"
    ready = final / "server/build/main.js"
    if ready.is_file() and (final / ".musix_verified_commit").is_file():
        if (final / ".musix_verified_commit").read_text().strip() == PINNED_COMMIT:
            return ready

    node_bin = _node_tools()
    env = dict(os.environ)
    env["PATH"] = str(node_bin) + os.pathsep + env.get("PATH", "")
    stage = home / f".bgutil-{VERSION}-install"
    if stage.exists():
        shutil.rmtree(stage)
    try:
        _run(["git", "clone", "--depth", "1", "--branch", VERSION, "--single-branch", SOURCE, str(stage)])
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=stage, text=True,
        ).strip()
        if sha != PINNED_COMMIT:
            raise RuntimeError("Upstream tag does not match pinned provider commit")

        _run(["npm", "ci", "--no-audit", "--no-fund"], cwd=stage / "server", env=env)
        _run(["npx", "--no-install", "tsc"], cwd=stage / "server", env=env)
        if not (stage / "server/build/main.js").is_file():
            raise RuntimeError("PO-token server build missing main.js")
        (stage / ".musix_verified_commit").write_text(PINNED_COMMIT + "\n")

        if final.exists():
            shutil.rmtree(final)
        stage.rename(final)
        return final / "server/build/main.js"
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    try:
        result = install()
        print("musix_pot_setup=ready version=" + VERSION + " file=" + str(result))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        # Exception categories only; do not leak shell commands or credentials.
        print("musix_pot_setup=failed type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
