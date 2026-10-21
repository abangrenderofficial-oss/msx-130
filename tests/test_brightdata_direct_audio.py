import io
import pytest

from services.media.brightdata_direct_audio import (
    AudioFormat, DirectAudioError, ranked_audio_formats, choose_downloaded_audio,
    parse_ump_audio, _video_id,
)


def test_selection_uses_true_source_bitrate_not_itag_or_mp3_target():
    streams = [
        {"itag": 140, "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "bitrate": 130555, "contentLength": "100"},
        {"itag": 249, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 60694, "contentLength": "80"},
        {"itag": 251, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 147790, "contentLength": "110"},
        {"itag": 250, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 78320, "contentLength": "90"},
        {"itag": 999, "mimeType": "video/webm", "bitrate": 999999, "contentLength": "1000"},
    ]
    ranked = ranked_audio_formats(streams)
    assert [f.itag for f in ranked] == [251, 140, 250, 249]
    selected, raw = choose_downloaded_audio(streams, {(251,0):b"a"*10,(251,1):b"b"*100,(140,0):b"c"*100})
    assert selected.itag == 251
    assert len(raw) == 110


def test_selector_rejects_incomplete_and_selects_highest_complete():
    formats = [
        {"itag": 251, "mimeType": "audio/webm; codecs=opus", "bitrate": 145000, "contentLength": 110},
        {"itag": 140, "mimeType": "audio/mp4; codecs=aac", "bitrate": 132000, "contentLength": 100},
    ]
    selected, _ = choose_downloaded_audio(formats, {(251,0):b"a"*50, (251,2):b"b"*60,(140,0):b"x"*100})
    assert selected.itag == 140
    with pytest.raises(DirectAudioError):
        choose_downloaded_audio(formats, {(251,0):b"a"*50,(251,2):b"b"*60})


def test_video_id_allowlist():
    assert _video_id("https://www.youtube.com/watch?v=I1zqCX3Dvxo") == "I1zqCX3Dvxo"
    assert _video_id("https://youtu.be/I1zqCX3Dvxo") == "I1zqCX3Dvxo"
    assert _video_id("https://youtube.com/shorts/I1zqCX3Dvxo") == "I1zqCX3Dvxo"
    with pytest.raises(DirectAudioError):
        _video_id("https://invalid.example/watch?v=I1zqCX3Dvxo")


def test_ump_parser_matches_real_response_when_saved(tmp_path):
    from pathlib import Path
    sample = Path("/tmp/bd_raw_application_vnd.yt-ump_1.bin")
    if not sample.is_file():
        pytest.skip("No live test UMP file")
    parts = parse_ump_audio(sample.read_bytes())
    combined = b"".join(chunk for (itag, _), chunk in sorted(parts.items()) if itag == 251)
    assert len(combined) == 255427
    assert combined.startswith(bytes.fromhex("1a45dfa3"))
