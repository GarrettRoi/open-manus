"""Long-audio regression tests for VoiceSink (Task: long voice messages losing early audio).

Verifies:
  - No audio bytes are dropped, even when packets arrive while a previous
    chunk is being transcribed (double-buffering).
  - Long audio is proactively flushed into multiple chunks before hitting
    the STT size cap, and uploads are downsampled to 16kHz mono.
  - Chunk transcripts are stitched in order into a single dispatched message.
  - A failed chunk is retried, and an ultimate failure produces a visible
    error instead of silent loss.
"""

import asyncio
import importlib
import sys
import wave

import pytest


@pytest.fixture(scope="module")
def vs_mod():
    """Import voice_sink with discord-ext-voice-recv blocked.

    The gateway conftest installs a MagicMock ``discord`` module, which would
    make ``VoiceSink`` subclass a MagicMock and mock away all real behavior.
    Force the ImportError fallback (``_AudioSinkBase = object``) so the tests
    exercise the real implementation.
    """
    orig_mod = sys.modules.pop("gateway.platforms.voice_sink", None)
    orig_ext = sys.modules.pop("discord.ext", None)
    orig_recv = sys.modules.pop("discord.ext.voice_recv", None)
    sys.modules["discord.ext"] = None  # None in sys.modules -> ImportError
    try:
        mod = importlib.import_module("gateway.platforms.voice_sink")
    finally:
        sys.modules.pop("discord.ext", None)
        if orig_ext is not None:
            sys.modules["discord.ext"] = orig_ext
        if orig_recv is not None:
            sys.modules["discord.ext.voice_recv"] = orig_recv
        sys.modules.pop("gateway.platforms.voice_sink", None)
        if orig_mod is not None:
            sys.modules["gateway.platforms.voice_sink"] = orig_mod
    assert mod._VOICE_RECV_OK is False
    return mod


SAMPLE_RATE = 48000
BYTES_PER_SEC = SAMPLE_RATE * 2 * 2  # 48kHz stereo 16-bit


class FakeUser:
    def __init__(self, uid=111, name="tester"):
        self.id = uid
        self.bot = False
        self.display_name = name


class FakeVoiceData:
    def __init__(self, pcm: bytes):
        self.pcm = pcm


class FakeClient:
    def __init__(self):
        self.user = None

    def get_user(self, uid):
        return None

    def get_channel(self, cid):
        return None

    async def fetch_user(self, uid):
        raise RuntimeError("no network in tests")

    async def fetch_channel(self, cid):
        raise RuntimeError("no network in tests")


class FakeAdapter:
    def __init__(self):
        self._client = FakeClient()
        self.dispatched = []

    def build_source(self, **kwargs):
        return kwargs

    async def handle_message(self, event):
        self.dispatched.append(event)


@pytest.fixture
def sink(monkeypatch, vs_mod):
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "test-key")
    monkeypatch.delenv("DISCORD_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("VOICE_SILENCE_THRESHOLD", "0.5")
    monkeypatch.setenv("VOICE_MAX_CHUNK_SECONDS", "1")  # 1s chunks = 192000 bytes
    monkeypatch.setenv("VOICE_TRANSCRIBE_RETRIES", "1")
    monkeypatch.setenv("VOICE_TRANSCRIBE_RETRY_DELAY", "0.05")
    adapter = FakeAdapter()
    s = vs_mod.VoiceSink(adapter, "999")
    return s


def _install_fake_transcriber(monkeypatch, vs_mod, records, results=None, delay=0.0):
    """Replace the lazily-loaded transcribe function.

    ``records`` collects (n_frames, framerate, channels) per call.
    ``results`` is an optional list of dicts returned per call (cycled last).
    """
    calls = {"n": 0}

    def fake_transcribe(file_path, model=None):
        import time as _t
        if delay:
            _t.sleep(delay)
        with wave.open(file_path, "rb") as w:
            records.append((w.getnframes(), w.getframerate(), w.getnchannels()))
        idx = calls["n"]
        calls["n"] += 1
        if results:
            return results[min(idx, len(results) - 1)]
        return {"success": True, "transcript": f"chunk{idx}"}

    monkeypatch.setattr(vs_mod, "_transcribe_audio", fake_transcribe)
    return calls


def _feed(sink, user, seconds, packet_ms=20):
    """Feed synthetic PCM through write() in packet-sized pieces."""
    packet = b"\x01\x02" * (SAMPLE_RATE // (1000 // packet_ms) * 2)  # stereo 16-bit
    n_packets = int(seconds * 1000 / packet_ms)
    for _ in range(n_packets):
        sink.write(user, FakeVoiceData(packet))
    return len(packet) * n_packets


def test_long_audio_chunked_no_bytes_dropped_and_stitched(sink, monkeypatch, vs_mod):
    """3.5s of audio with 1s chunk cap → multiple ordered chunks, one message."""
    records = []
    _install_fake_transcriber(monkeypatch, vs_mod, records)
    user = FakeUser()

    async def run():
        sink.start()
        total_bytes = _feed(sink, user, 3.5)
        # Let the monitor flush size-based chunks, then go silent for final flush
        await asyncio.sleep(2.5)
        sink._running = False
        return total_bytes

    total_bytes = asyncio.run(run())

    # Everything downsampled to 16kHz mono
    assert records, "transcriber never called"
    assert all(rate == 16000 and ch == 1 for _, rate, ch in records)

    # No bytes dropped: input frames (stereo 48k) == sum of output frames * 3
    input_frames = total_bytes // 4  # 4 bytes per stereo frame
    output_frames = sum(n for n, _, _ in records)
    assert abs(output_frames * 3 - input_frames) <= 3 * len(records)

    # Chunking triggered: >1 chunk for 3.5s audio with 1s cap
    assert len(records) >= 2

    # One coherent dispatched message with transcripts stitched in order
    assert len(sink.adapter.dispatched) == 1
    text = sink.adapter.dispatched[0].text
    expected = "[VOICE] " + " ".join(f"chunk{i}" for i in range(len(records)))
    assert text == expected


def test_audio_during_transcription_not_dropped(sink, monkeypatch, vs_mod):
    """Packets written while a chunk is transcribing are buffered, not dropped."""
    records = []
    _install_fake_transcriber(monkeypatch, vs_mod, records, delay=0.3)
    user = FakeUser()

    async def run():
        sink.start()
        _feed(sink, user, 1.5)  # exceeds 1s cap -> size flush starts
        await asyncio.sleep(0.35)  # monitor triggers, transcription in flight
        assert sink.is_processing.get(user.id)
        _feed(sink, user, 1.0)  # speech during transcription
        await asyncio.sleep(3.0)  # let everything settle + final flush
        sink._running = False

    asyncio.run(run())

    total_input_frames = int(2.5 * SAMPLE_RATE)
    output_frames = sum(n for n, _, _ in records)
    # Allow tiny rounding slack from packetization/ratecv
    assert abs(output_frames * 3 - total_input_frames) <= SAMPLE_RATE * 0.1
    assert len(sink.adapter.dispatched) == 1


def test_failed_chunk_retried_then_visible_error(sink, monkeypatch, vs_mod):
    """Failure is retried; ultimate failure surfaces a visible message."""
    records = []
    _install_fake_transcriber(
        monkeypatch, vs_mod, records,
        results=[{"success": False, "transcript": "", "error": "boom"}],
    )
    debug_msgs = []

    async def capture_debug(text):
        debug_msgs.append(text)

    sink._send_debug_message = capture_debug
    user = FakeUser()

    async def run():
        sink.start()
        _feed(sink, user, 0.8)
        await asyncio.sleep(1.5)
        sink._running = False

    asyncio.run(run())

    # retried: VOICE_TRANSCRIBE_RETRIES=1 -> 2 attempts
    assert len(records) == 2
    assert any("Transcription failed" in m for m in debug_msgs)
    assert not sink.adapter.dispatched


def test_partial_success_still_dispatched_on_final_failure(sink, monkeypatch, vs_mod):
    """If a later chunk fails, earlier successful chunks are still delivered."""
    records = []
    _install_fake_transcriber(
        monkeypatch, vs_mod, records,
        results=[
            {"success": True, "transcript": "early part"},
            {"success": False, "transcript": "", "error": "boom"},
        ],
    )
    debug_msgs = []

    async def capture_debug(text):
        debug_msgs.append(text)

    sink._send_debug_message = capture_debug
    user = FakeUser()

    async def run():
        sink.start()
        _feed(sink, user, 1.5)  # 1s cap -> first chunk flushes, rest is final
        await asyncio.sleep(2.5)
        sink._running = False

    asyncio.run(run())

    assert len(sink.adapter.dispatched) == 1
    assert sink.adapter.dispatched[0].text == "[VOICE] early part"
    assert any("Transcription failed" in m for m in debug_msgs)


def test_short_tail_after_cap_chunk_not_dropped(sink, monkeypatch, vs_mod):
    """A <250ms tail after a cap-sized chunk is merged, not discarded."""
    records = []
    _install_fake_transcriber(monkeypatch, vs_mod, records)
    user = FakeUser()

    async def run():
        sink.start()
        _feed(sink, user, 1.1)  # 1s cap chunk + 0.1s tail (< MIN_AUDIO_BYTES)
        await asyncio.sleep(1.5)
        sink._running = False

    asyncio.run(run())

    input_frames = int(1.1 * SAMPLE_RATE)
    output_frames = sum(n for n, _, _ in records)
    # No bytes dropped: the tail may be padded with silence up to the 0.25s
    # minimum, so output can slightly exceed input but never fall short.
    assert output_frames * 3 >= input_frames - 3 * max(1, len(records))
    assert output_frames * 3 <= input_frames + int(0.25 * SAMPLE_RATE) + 3
    assert len(sink.adapter.dispatched) == 1


def test_raising_transcriber_is_retried(sink, monkeypatch, vs_mod):
    """An exception from the transcriber counts as a failed attempt and is retried."""
    calls = {"n": 0}

    def flaky(file_path, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient network blip")
        return {"success": True, "transcript": "recovered text"}

    monkeypatch.setattr(vs_mod, "_transcribe_audio", flaky)
    user = FakeUser()

    async def run():
        sink.start()
        _feed(sink, user, 0.8)
        await asyncio.sleep(1.5)
        sink._running = False

    asyncio.run(run())

    assert calls["n"] == 2
    assert len(sink.adapter.dispatched) == 1
    assert sink.adapter.dispatched[0].text == "[VOICE] recovered text"
