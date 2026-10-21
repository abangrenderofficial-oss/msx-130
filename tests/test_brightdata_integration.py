"""Bright Data smart audio fallback must never break original Musix routes."""
import pytest
from services.media import music_download, brightdata_direct_audio


@pytest.mark.asyncio
async def test_brightdata_enabled_calls_smart_audio_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED", "0")
    monkeypatch.setenv("MUSIX_YOUTUBE_LOCAL_FIRST", "0")
    monkeypatch.setenv("MUSIX_BRIGHTDATA_DIRECT_ENABLED", "1")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WS", "configured-secret-hidden")
    called = []
    async def fake_smart(url, out_template, kbps):
        called.append((url, out_template, kbps))
        return str(tmp_path / "source.mp3")
    monkeypatch.setattr(brightdata_direct_audio, "download_best_audio_mp3", fake_smart)
    monkeypatch.setattr(music_download, "_youtube_worker_configured", lambda: (_ for _ in ()).throw(AssertionError("legacy worker must not run after smart success")))
    result = await music_download._download_mp3(
        "https://www.youtube.com/watch?v=I1zqCX3Dvxo",
        work_dir=str(tmp_path), bitrate_kbps=192, source="youtube",
    )
    assert result.endswith("source.mp3")
    assert called and called[0][-1] == 192


@pytest.mark.asyncio
async def test_smart_audio_failure_preserves_old_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED", "0")
    monkeypatch.setenv("MUSIX_YOUTUBE_LOCAL_FIRST", "0")
    monkeypatch.setenv("MUSIX_BRIGHTDATA_DIRECT_ENABLED", "1")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WS", "configured-secret-hidden")
    async def fail_smart(url, out_template, kbps):
        raise brightdata_direct_audio.DirectAudioError("Remote Chrome temporarily unavailable")
    monkeypatch.setattr(brightdata_direct_audio, "download_best_audio_mp3", fail_smart)
    monkeypatch.setattr(music_download, "_youtube_worker_configured", lambda: False)
    monkeypatch.setattr(music_download, "_cobalt_music_configured", lambda: False)
    monkeypatch.setattr(music_download, "_configured_invidious_api_urls", lambda: [])
    monkeypatch.setattr(music_download, "_run_piped_mp3_sync", lambda *_: str(tmp_path / "piped.mp3"))
    result = await music_download._download_mp3(
        "https://www.youtube.com/watch?v=I1zqCX3Dvxo",
        work_dir=str(tmp_path), bitrate_kbps=192, source="youtube",
    )
    assert result.endswith("piped.mp3")


@pytest.mark.asyncio
async def test_smart_audio_disabled_never_calls_paid_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIX_YOUTUBE_VIDEO_FIRST_ENABLED", "0")
    monkeypatch.setenv("MUSIX_YOUTUBE_LOCAL_FIRST", "0")
    monkeypatch.setenv("MUSIX_BRIGHTDATA_DIRECT_ENABLED", "0")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WS", "configured-secret-hidden")
    async def should_not_call(*_args):
        raise AssertionError("Disabled paid browser must never run")
    monkeypatch.setattr(brightdata_direct_audio, "download_best_audio_mp3", should_not_call)
    monkeypatch.setattr(music_download, "_youtube_worker_configured", lambda: False)
    monkeypatch.setattr(music_download, "_cobalt_music_configured", lambda: False)
    monkeypatch.setattr(music_download, "_configured_invidious_api_urls", lambda: [])
    monkeypatch.setattr(music_download, "_run_piped_mp3_sync", lambda *_: str(tmp_path / "piped.mp3"))
    result = await music_download._download_mp3(
        "https://www.youtube.com/watch?v=I1zqCX3Dvxo",
        work_dir=str(tmp_path), bitrate_kbps=192, source="youtube",
    )
    assert result.endswith("piped.mp3")
