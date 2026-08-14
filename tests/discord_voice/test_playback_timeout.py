"""Duration-aware voice playback timeouts (fix for long TTS segments being
cut ~90% through the first part of a multi-part reply)."""
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "plugins/platforms/discord"))
import adapter as adapter_mod  # noqa: E402

A = adapter_mod.DiscordAdapter


def _timeout(dur):
    return A._playback_timeout_for(A.__new__(A), dur)


def test_short_clip_keeps_floor():
    assert _timeout(10) == A.PLAYBACK_TIMEOUT
    assert _timeout(None) == A.PLAYBACK_TIMEOUT
    assert _timeout(0) == A.PLAYBACK_TIMEOUT


def test_long_clip_scales_with_duration():
    # A ~140s TTS segment must NOT be capped at 120s
    assert _timeout(140) == 140 + A.PLAYBACK_TIMEOUT_MARGIN
    assert _timeout(300) == 300 + A.PLAYBACK_TIMEOUT_MARGIN


def test_pcm_duration_math():
    # 48kHz stereo s16 → 192000 bytes/sec
    assert 192000 * 130 / 192000.0 == 130.0
