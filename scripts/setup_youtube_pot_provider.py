"""Install pinned bgutil PO Token Provider on Bot Hosting, without secrets.

One-time command:
    python3 scripts/setup_youtube_pot_provider.py
Idempotent; files persist outside the Git checkout. The bot starts its own
private Deno process lazily on demand. No npm, public port or bot token needed.
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


def _deno_path() -> str:
    bundled = pathlib.Path("/home/container/bin/deno")
    if bundled.is_file():
        return str(bundled)
    executable = shutil.which("deno")
    if executable:
        return executable
    raise RuntimeError("Deno is unavailable on this deployment")


def _run(command: list[str], *, cwd: pathlib.Path | None = None) -> None:
    subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        timeout=180,
        stdout=subprocess.DEVNULL,
    )


def install() -> pathlib.Path:
    home = pathlib.Path(os.getenv("MUSIX_POT_HOME", DEFAULT_HOME)).resolve()
    home.mkdir(parents=True, exist_ok=True)
    final = home / f"bgutil-{VERSION}"
    entry = final / "server/src/main.ts"
    marker = final / ".musix_verified_commit"
    if entry.is_file() and marker.is_file() and (final / "server/node_modules").is_dir():
        if marker.read_text().strip() == PINNED_COMMIT:
            return entry

    stage = home / f".bgutil-{VERSION}-install"
    if stage.exists():
        shutil.rmtree(stage)
    try:
        _run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1",
              "--branch", VERSION, "--single-branch", SOURCE, str(stage)])
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=stage, text=True,
        ).strip()
        if sha != PINNED_COMMIT:
            raise RuntimeError("Upstream tag differs from pinned source commit")

        _run([_deno_path(), "install", "--allow-scripts=npm:canvas", "--frozen"],
             cwd=stage / "server")
        if not (stage / "server/src/main.ts").is_file():
            raise RuntimeError("PO-token server source is incomplete")
        if not (stage / "server/node_modules").is_dir():
            raise RuntimeError("Provider dependencies not installed")
        (stage / ".musix_verified_commit").write_text(PINNED_COMMIT + "\n")

        if final.exists():
            shutil.rmtree(final)
        stage.rename(final)
        return final / "server/src/main.ts"
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    try:
        result = install()
        print("musix_pot_setup=ready version=" + VERSION + " file=" + str(result))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print("musix_pot_setup=failed type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
