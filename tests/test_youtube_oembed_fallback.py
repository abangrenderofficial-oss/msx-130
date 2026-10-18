"""YouTube public metadata must not prevent independent audio fallback attempts.

oEmbed contains a title only. It never claims a stream or successful download.
"""
import httpx
import pytest

from services.media import music_download as music


def test_official_youtube_oembed_provides_title_not_audio(monkeypatch):
    original_client = httpx.Client
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={
            "title": "My Sample Video",
            "author_name": "My Sample Creator",
        })

    monkeypatch.setattr(
        music.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    info = music._youtube_oembed_info_sync(
        "https://youtu.be/jNQXAC9IVRw"
    )
    assert info["id"] == "jNQXAC9IVRw"
    assert info["title"] == "My Sample Video"
    assert info["uploader"] == "My Sample Creator"
    assert "url" not in info
    assert "formats" not in info
    assert "duration" not in info
    assert seen[0].url.host == "www.youtube.com"
    assert seen[0].url.path == "/oembed"


def test_metadata_login_required_tries_oembed_without_claiming_audio(monkeypatch):
    monkeypatch.setenv("MUSIX_YOUTUBE_OEMBED_FALLBACK_ENABLED", "1")
    attempts = []

    def fake_ytdlp(url, *, youtube_client=None):
        attempts.append(youtube_client)
        raise RuntimeError("YouTube: Sign in to confirm you're not a bot")

    monkeypatch.setattr(music, "_extract_info_once", fake_ytdlp)
    monkeypatch.setattr(music, "_youtube_low_memory_mode", lambda: False)
    monkeypatch.setattr(music, "_youtube_oembed_info_sync",
                        lambda url: {"id": "jNQXAC9IVRw", "title": "Public title"})
    result = music._extract_info_sync("https://youtu.be/jNQXAC9IVRw", "youtube")
    assert result == {"id": "jNQXAC9IVRw", "title": "Public title"}
    assert len(attempts) > 1


def test_oembed_failure_keeps_real_audio_error(monkeypatch):
    monkeypatch.setattr(music, "_extract_info_once", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sign-in")))
    monkeypatch.setattr(music, "_youtube_low_memory_mode", lambda: True)
    monkeypatch.setattr(music, "_configured_invidious_api_urls", lambda: [])
    monkeypatch.setattr(music, "_configured_piped_api_urls", lambda: [])
    monkeypatch.setattr(music, "_youtube_oembed_info_sync", lambda url: (_ for _ in ()).throw(RuntimeError("oembed offline")))
    with pytest.raises(music.MusicDownloadError, match="YouTube low-memory metadata retries"):
        music._extract_info_sync("https://youtu.be/jNQXAC9IVRw", "youtube")


def test_non_youtube_never_calls_youtube_oembed(monkeypatch):
    def broken_oembed(url):
        raise AssertionError("oEmbed must never run for TikTok/other sources")
    monkeypatch.setattr(music, "_youtube_oembed_info_sync", broken_oembed)
    monkeypatch.setattr(music, "_extract_info_once", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upstream")))
    with pytest.raises(music.MusicDownloadError, match="upstream"):
        music._extract_info_sync("https://www.tiktok.com/@x/video/123", "tiktok")


def test_oembed_never_activates_while_media_backends_are_unverified(monkeypatch):
    monkeypatch.delenv("MUSIX_YOUTUBE_OEMBED_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(music, "_youtube_low_memory_mode", lambda: True)
    monkeypatch.setattr(music, "_configured_invidious_api_urls", lambda: [])
    monkeypatch.setattr(music, "_configured_piped_api_urls", lambda: [])
    monkeypatch.setattr(
        music, "_extract_info_once",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("login required")),
    )
    monkeypatch.setattr(
        music, "_youtube_oembed_info_sync",
        lambda url: (_ for _ in ()).throw(AssertionError("opt-in required")),
    )
    with pytest.raises(music.MusicDownloadError, match="YouTube low-memory metadata retries"):
        music._extract_info_sync("https://youtu.be/jNQXAC9IVRw", "youtube")
