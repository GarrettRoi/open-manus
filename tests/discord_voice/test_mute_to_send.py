"""Mute-to-send: unmuted users are held through pauses; muting flushes."""
import sys, pathlib, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "plugins/platforms/discord"))
import adapter as adapter_mod  # noqa: E402

VR = adapter_mod.VoiceReceiver
BYTES_PER_SEC = 48000 * 2 * 2


def _receiver():
    r = VR.__new__(VR)
    import threading
    from collections import defaultdict
    r._lock = threading.Lock()
    r._ssrc_to_user = {}
    r._buffers = defaultdict(bytearray)
    r._last_packet_time = {}
    r._user_muted = {}
    r._flush_users = set()
    r._allowed_user_ids = set()
    return r


def _speech(r, ssrc, user, seconds, last_packet_age):
    r._ssrc_to_user[ssrc] = user
    r._buffers[ssrc] = bytearray(b"\x00" * int(BYTES_PER_SEC * seconds))
    r._last_packet_time[ssrc] = time.monotonic() - last_packet_age


def test_classic_silence_flush_for_unknown_state():
    r = _receiver()
    _speech(r, 1, 111, seconds=2, last_packet_age=2.0)  # > 1.5s silence
    out = r.check_silence()
    assert [u for u, _ in out] == [111]


def test_unmuted_user_held_through_short_pause():
    r = _receiver()
    r.set_user_muted(222, False)  # unmuted → still composing
    _speech(r, 2, 222, seconds=3, last_packet_age=3.0)  # 3s pause < 10s hold
    assert r.check_silence() == []
    assert len(r._buffers[2]) > 0  # buffer retained


def test_mute_transition_flushes_immediately():
    r = _receiver()
    r.set_user_muted(333, False)
    _speech(r, 3, 333, seconds=4, last_packet_age=0.2)  # still "talking"
    r.set_user_muted(333, True)  # mute = done talking
    out = r.check_silence()
    assert [u for u, _ in out] == [333]
    assert len(r._buffers[3]) == 0


def test_unmuted_hold_eventually_flushes():
    r = _receiver()
    r.set_user_muted(444, False)
    _speech(r, 4, 444, seconds=5, last_packet_age=VR.UNMUTED_SILENCE_THRESHOLD + 1)
    out = r.check_silence()
    assert [u for u, _ in out] == [444]


def test_mute_flush_skips_noise_below_min_duration():
    r = _receiver()
    r.set_user_muted(555, False)
    _speech(r, 5, 555, seconds=0.2, last_packet_age=0.1)  # below MIN_SPEECH_DURATION
    r.set_user_muted(555, True)
    assert r.check_silence() == []
    assert len(r._buffers[5]) == 0  # cleared, not delivered
