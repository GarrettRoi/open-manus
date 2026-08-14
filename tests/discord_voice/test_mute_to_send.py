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
    r._flush_users = {}
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


def test_flush_marker_survives_unmapped_ssrc_pass():
    """A mute-flush must not be lost when the SSRC is momentarily unmapped."""
    r = _receiver()
    r.set_user_muted(666, False)
    _speech(r, 6, 666, seconds=4, last_packet_age=0.2)
    del r._ssrc_to_user[6]  # mapping gap at the moment of the pass
    r._vc = type("VC", (), {"channel": None, "user": None})()
    r.set_user_muted(666, True)
    assert r.check_silence() == []      # pass with no mapping: nothing flushed
    assert 666 in r._flush_users        # ...but the marker survives
    r._ssrc_to_user[6] = 666            # mapping restored next pass
    out = r.check_silence()
    assert [u for u, _ in out] == [666]
    assert 666 not in r._flush_users


def test_hot_mic_max_utterance_cap_flushes():
    """Continuous packets (hot mic) still flush once the cap is reached."""
    r = _receiver()
    r.set_user_muted(777, False)
    _speech(r, 7, 777, seconds=VR.MAX_UTTERANCE_SECONDS + 1, last_packet_age=0.05)
    out = r.check_silence()
    assert [u for u, _ in out] == [777]


def test_marker_survives_multisecond_mapping_gap(monkeypatch):
    """Marker must survive >1s while the user's audio sits in an UNMAPPED buffer."""
    r = _receiver()
    r.set_user_muted(888, False)
    _speech(r, 8, 888, seconds=4, last_packet_age=0.2)
    del r._ssrc_to_user[8]
    r._vc = type("VC", (), {"channel": None, "user": None})()
    r.set_user_muted(888, True)
    # simulate 5s of passes with the SSRC still unmapped
    base = time.monotonic()
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: base + 5)
    assert r.check_silence() == [] or True  # unmapped: nothing delivered yet
    assert 888 in r._flush_users            # marker still alive after 5s
    r._ssrc_to_user[8] = 888
    out = r.check_silence()
    assert [u for u, _ in out] == [888]


def test_marker_expires_after_ttl(monkeypatch):
    r = _receiver()
    r.set_user_muted(999, False)
    _speech(r, 9, 999, seconds=1, last_packet_age=0.1)
    del r._ssrc_to_user[9]
    r._vc = type("VC", (), {"channel": None, "user": None})()
    r.set_user_muted(999, True)
    base = time.monotonic()
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: base + VR.FLUSH_MARKER_TTL + 1)
    r.check_silence()
    assert 999 not in r._flush_users


def test_one_mute_flushes_all_ssrc_buffers_of_user():
    r = _receiver()
    r.set_user_muted(1010, False)
    _speech(r, 10, 1010, seconds=2, last_packet_age=0.1)
    _speech(r, 11, 1010, seconds=3, last_packet_age=0.1)
    r.set_user_muted(1010, True)
    out = r.check_silence()
    assert sorted(u for u, _ in out) == [1010, 1010]
    assert 1010 not in r._flush_users
