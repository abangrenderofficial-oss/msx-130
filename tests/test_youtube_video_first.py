"""Video-first pipeline: selection, fail-closed verification and real FFmpeg."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from services.media import youtube_video_first as video_first


def _v(fid, *, height=360, abr=None, size=1000, vcodec="h264", acodec="aac"):
    return {
        "format_id": fid,
        "height": height,
        "abr": abr,
        "filesize": size,
        "vcodec": vcodec,
        "acodec": acodec,
    }


def test_combined_video_selected_for_best_audio_then_smaller_size():
    formats = [
        _v("large", height=1080, abr=128, size=100_000),
        _v("low", height=360, abr=96, size=7000),
        _v("winner", height=360, abr=128, size=6000),
        _v("larger", height=360, abr=128, size=9000),
        _v("silent", acodec="none", size=100),
    ]
    assert video_first._media_formats({"formats": formats}, max_height=360) == "winner"


def test_separate_video_and_audio_are_merged_when_no_combined_stream():
    formats = [
        _v("720", height=720, acodec="none", size=8000),
        _v("144", height=144, acodec="none", size=900),
        _v("aac64", vcodec="none", abr=64, size=2000),
        _v("aac128", vcodec="none", abr=128, size=3000),
    ]
    assert video_first._media_formats({"formats": formats}, max_height=360) == "144+aac128"


def test_rejects_video_without_audio():
    with pytest.raises(video_first.VideoFirstError, match="audio track"):
        video_first._media_formats(
            {"formats": [_v("silent", acodec="none")]}, max_height=360
        )


def test_never_accepts_audio_only_without_video():
    with pytest.raises(video_first.VideoFirstError, match="video"):
        video_first._media_formats(
            {"formats": [_v("audio", vcodec="none")]}, max_height=360
        )


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="real FFmpeg and FFprobe not installed",
)
def test_real_video_to_mp3_then_video_cleanup(tmp_path):
    class LocalDownloader:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return {"formats": [_v("18", abr=96, size=1000)]}

        def download(self, _urls):
            file_path = self.opts["outtmpl"].replace("%(ext)s", "mkv")
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-t", "1", "-c:v", "mpeg4", "-c:a", "aac",
                    "-shortest", file_path,
                ],
                capture_output=True, text=True, timeout=25, check=False,
            )
            assert result.returncode == 0, result.stderr

    output = video_first._run_video_first(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        str(tmp_path / "source.%(ext)s"),
        192,
        ydl_factory=LocalDownloader,
    )
    final = Path(output)
    assert final.name == "source.mp3"
    assert final.exists() and final.stat().st_size > 0
    assert video_first._streams(video_first._probe(final), "audio")
    assert not list(tmp_path.glob("source-video.*"))


@pytest.mark.asyncio
async def test_opt_in_musix_routing_does_not_call_old_worker(monkeypatch, tmp_path):
    from services.media import music_download

    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED", "1")

    async def fake_video_first(url, out_template, bitrate):
        assert "youtu" in url and bitrate == 192
        return str(tmp_path / "source.mp3")

    def old_path(*_args, **_kwargs):
        raise AssertionError("Legacy worker must not run after video-first success")

    monkeypatch.setattr(video_first, "download_video_first_mp3", fake_video_first)
    monkeypatch.setattr(music_download, "_run_ytdlp_mp3_sync", old_path)
    monkeypatch.setattr(music_download, "_run_youtube_worker_mp3_sync", old_path)
    output = await music_download._download_mp3(
        "https://youtu.be/dQw4w9WgXcQ",
        work_dir=str(tmp_path),
        bitrate_kbps=192,
        source="youtube",
    )
    assert output.endswith("source.mp3")
