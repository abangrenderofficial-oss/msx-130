"""Self-hosted PO Token Provider lifecycle tests (no YouTube account required)."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import pytest

from services.media import youtube_pot_lifecycle as lifecycle
from services.media import youtube_video_first


def test_provider_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MUSIX_VIDEO_FIRST_POT_AUTO_START", raising=False)
    assert lifecycle.ensure_local_provider() is None


def test_private_provider_uses_only_loopback_and_reuses_healthy_process(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_VIDEO_FIRST_POT_AUTO_START", "1")
    monkeypatch.setenv("MUSIX_POT_HOME", str(tmp_path))
    entry = tmp_path / "bgutil-2.0.2/server/build/main.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// test fixture")
    state = {"healthy": False, "spawns": []}
    monkeypatch.setattr(lifecycle, "_healthcheck", lambda: state["healthy"])
    monkeypatch.setattr(lifecycle, "_node_executable", lambda: Path("/fake/bin/node"))

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            state["healthy"] = False

        def wait(self, timeout=3):
            return 0

        def kill(self):
            return None

    def spawn(argv, **kwargs):
        state["spawns"].append((argv, kwargs))
        state["healthy"] = True
        return FakeProcess()

    monkeypatch.setattr(lifecycle.subprocess, "Popen", spawn)
    monkeypatch.setattr(lifecycle, "_child", None)
    try:
        assert lifecycle.ensure_local_provider() == "http://127.0.0.1:4416"
        assert lifecycle.ensure_local_provider() == "http://127.0.0.1:4416"
        assert len(state["spawns"]) == 1
        argv, kwargs = state["spawns"][0]
        assert argv[-2:] == ["--host", "127.0.0.1"]
        assert kwargs["cwd"] == str(entry.parent.parent)
    finally:
        lifecycle._stop()


def test_provider_missing_files_never_downloads_at_request_time(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_VIDEO_FIRST_POT_AUTO_START", "1")
    monkeypatch.setenv("MUSIX_POT_HOME", str(tmp_path))
    monkeypatch.setattr(lifecycle, "_healthcheck", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        lifecycle.ensure_local_provider()


def test_video_first_passes_autostart_provider_url(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSIX_VIDEO_FIRST_POT_PROVIDER_URL", raising=False)
    from services.media import youtube_pot_lifecycle

    monkeypatch.setattr(
        youtube_pot_lifecycle, "ensure_local_provider",
        lambda: "http://127.0.0.1:4416",
    )
    observed = []
    original = youtube_video_first.build_ytdlp_youtube_options

    def builder(**kwargs):
        observed.append(kwargs)
        return original(**kwargs)

    class MetadataOnly:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, *_args, **_kwargs):
            return {"formats": [{"format_id":"18", "vcodec":"h264", "acodec":"aac", "height":360}]}

        def download(self, _urls):
            raise RuntimeError("fixture stops before network access")

    monkeypatch.setattr(youtube_video_first, "build_ytdlp_youtube_options", builder)
    with pytest.raises(youtube_video_first.VideoFirstError, match="acquisition failed"):
        youtube_video_first._run_video_first(
            "https://youtu.be/dQw4w9WgXcQ",
            str(tmp_path / "source.%(ext)s"),
            128,
            ydl_factory=MetadataOnly,
        )
    assert len(observed) == 2
    assert all(
        k["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == ["http://127.0.0.1:4416"]
        for k in observed
    )
