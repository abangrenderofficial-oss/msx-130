from handlers.music import _clean_youtube_title, _friendly_music_error, _music_audio_caption, _social_audio_caption, _youtube_fast_worker_enabled


def test_clean_youtube_title_removes_lyrics_noise():
    assert _clean_youtube_title("Song Name (Official Lyrics)") == "Song Name"
    assert _clean_youtube_title("Song Name [Lirik Video]") == "Song Name"
    assert _clean_youtube_title("Song Name - Lyrics") == "Song Name"


def test_clean_youtube_title_keeps_normal_title():
    assert _clean_youtube_title("Song Name - Artist") == "Song Name - Artist"


def test_fast_worker_can_be_disabled_without_affecting_cache(monkeypatch):
    monkeypatch.delenv("MUSIX_YOUTUBE_FAST_WORKER_ENABLED", raising=False)
    assert _youtube_fast_worker_enabled() is True
    monkeypatch.setenv("MUSIX_YOUTUBE_FAST_WORKER_ENABLED", "0")
    assert _youtube_fast_worker_enabled() is False
    monkeypatch.setenv("MUSIX_YOUTUBE_FAST_WORKER_ENABLED", "1")
    assert _youtube_fast_worker_enabled() is True


def test_youtube_signin_from_public_video_reports_server_block():
    message = _friendly_music_error(RuntimeError(
        "ERROR: [youtube] video123: Please sign in. "
        "Use --cookies-from-browser for authentication."
    ))
    assert "YouTube" in message
    assert "server" in message
    assert "private" not in message.lower()


def test_youtube_caption_matches_requested_two_lines_without_bitrate():
    assert _music_audio_caption(
        "Wyw my sweetie #JerseyClub (SLOWED) - nissi and 25Hzz"
    ) == (
        "🎵 Wyw my sweetie #JerseyClub (SLOWED) - nissi\n"
        "@abangrender_musicdownloaderbot"
    )


def test_youtube_caption_strips_lyrics_but_keeps_legitimate_artists():
    assert _music_audio_caption("Song Name (Official Lyrics) - Alice and Bob") == (
        "🎵 Song Name - Alice and Bob\n@abangrender_musicdownloaderbot"
    )


def test_audio_caption_escapes_html_and_preserves_other_sources():
    assert _music_audio_caption("A&B <song>", service_name="youtube") == (
        "🎵 A&amp;B &lt;song&gt;\n@abangrender_musicdownloaderbot"
    )
    assert _social_audio_caption("tiktok", "Music & Dance", "320 kbps") == (
        "🎵 Music &amp; Dance\n@abangrender_musicdownloaderbot"
    )


def test_caption_is_idempotent_after_title_cleanup():
    assert _music_audio_caption(_clean_youtube_title("Song - Lyrics")) == (
        "🎵 Song\n@abangrender_musicdownloaderbot"
    )
