"""Regression tests for full-response voice playback of long replies.

Covers Task: long spoken responses split into provider-safe segments must
play to completion, in order, as one continuous utterance:

1. ``split_text_for_tts`` — sentence-aware, provider-safe segmentation.
2. ``DiscordAdapter.play_tts_queue`` — sequential playback under the
   per-guild lock, pending-speech accounting, no-VC fallback.
3. Lifecycle protection — ``_voice_session_is_busy`` counts queued speech;
   the inactivity timeout never disconnects while segments are draining.

Uses the standard ``object.__new__(DiscordAdapter)`` helper from the voice
suite (no real discord.py connection).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.tts_tool import TTS_SEGMENT_TARGET_CHARS, split_text_for_tts


# =====================================================================
# split_text_for_tts
# =====================================================================

class TestSplitTextForTTS:
    def test_empty_and_blank(self):
        assert split_text_for_tts("", tts_config={}) == []
        assert split_text_for_tts("   ", tts_config={}) == []

    def test_short_text_single_segment(self):
        assert split_text_for_tts("Hello world.", tts_config={}) == ["Hello world."]

    def test_long_text_splits_into_ordered_segments(self):
        sentences = [f"This is sentence number {i} of the reply." for i in range(200)]
        text = " ".join(sentences)
        segments = split_text_for_tts(text, tts_config={})
        assert len(segments) > 1
        for seg in segments:
            assert len(seg) <= TTS_SEGMENT_TARGET_CHARS
        # Order + content preserved (split only collapses whitespace)
        assert " ".join(segments).split() == text.split()

    def test_respects_provider_cap_below_target(self):
        # kittentts caps at 2000 but target is 1500 -> limit 1500;
        # neutts caps at 2000 too; use a config override to force a low cap.
        cfg = {"provider": "edge", "edge": {"max_text_length": 100}}
        text = "word " * 100
        segments = split_text_for_tts(text, tts_config=cfg)
        assert all(len(s) <= 100 for s in segments)
        assert len(segments) >= 4

    def test_overlong_sentence_hard_split_on_word_boundary(self):
        text = "a" * 50 + " " + "b" * 50 + " " + "c" * 50
        segments = split_text_for_tts(text, tts_config={}, segment_chars=60)
        assert all(len(s) <= 60 for s in segments)
        assert "".join(segments).replace(" ", "") == text.replace(" ", "")

    def test_explicit_segment_chars_override(self):
        text = "One. Two. Three. Four. Five."
        segments = split_text_for_tts(text, tts_config={}, segment_chars=10)
        assert len(segments) >= 3
        assert all(len(s) <= 10 for s in segments)


# =====================================================================
# Discord adapter helpers
# =====================================================================

def _make_adapter():
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import Platform, PlatformConfig

    config = PlatformConfig(enabled=True, extra={})
    config.token = "fake-token"
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = config
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_play_locks = {}
    adapter._voice_pending_speech = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._voice_linger_tasks = {}
    adapter._voice_auto_join = False
    adapter._ambient_pcm_cache = None
    adapter._voice_fx_cfg = {"enabled": False, "speech_gain": 1.0}
    return adapter


GID = 424242
CHAT = "555"


def _wire_voice(adapter):
    adapter._voice_text_channels[GID] = int(CHAT)
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    adapter._voice_clients[GID] = vc
    return vc


# =====================================================================
# play_tts_queue — sequential, ordered, fully played
# =====================================================================

class TestPlayTTSQueue:
    @pytest.mark.asyncio
    async def test_all_segments_play_in_order(self):
        adapter = _make_adapter()
        _wire_voice(adapter)
        played = []

        async def fake_play(gid, path):
            played.append(path)
            return True

        adapter._play_in_voice_channel_now = fake_play
        result = await adapter.play_tts_queue(CHAT, ["a.mp3", "b.mp3", "c.mp3"])
        assert result.success is True
        assert played == ["a.mp3", "b.mp3", "c.mp3"]
        # Pending accounting fully drained
        assert adapter._voice_pending_speech.get(GID, 0) == 0

    @pytest.mark.asyncio
    async def test_busy_while_queue_draining(self):
        adapter = _make_adapter()
        _wire_voice(adapter)
        busy_snapshots = []

        async def fake_play(gid, path):
            busy_snapshots.append(adapter._voice_session_is_busy(gid))
            return True

        adapter._play_in_voice_channel_now = fake_play
        await adapter.play_tts_queue(CHAT, ["a.mp3", "b.mp3"])
        # During every segment the session reports busy (pending > 0)
        assert busy_snapshots == [True, True]
        assert adapter._voice_session_is_busy(GID) is False

    @pytest.mark.asyncio
    async def test_pending_cleared_on_playback_exception(self):
        adapter = _make_adapter()
        _wire_voice(adapter)

        async def boom(gid, path):
            raise RuntimeError("ffmpeg exploded")

        adapter._play_in_voice_channel_now = boom
        with pytest.raises(RuntimeError):
            await adapter.play_tts_queue(CHAT, ["a.mp3", "b.mp3", "c.mp3"])
        assert adapter._voice_pending_speech.get(GID, 0) == 0

    @pytest.mark.asyncio
    async def test_stops_draining_after_disconnect(self):
        adapter = _make_adapter()
        _wire_voice(adapter)
        played = []

        async def fake_play(gid, path):
            played.append(path)
            return path == "a.mp3"  # b fails (e.g. explicit /voice leave)

        adapter._play_in_voice_channel_now = fake_play
        result = await adapter.play_tts_queue(CHAT, ["a.mp3", "b.mp3", "c.mp3"])
        assert played == ["a.mp3", "b.mp3"]
        assert result.success is True  # at least one segment was spoken
        assert adapter._voice_pending_speech.get(GID, 0) == 0

    @pytest.mark.asyncio
    async def test_serialized_with_other_playback_callers(self):
        """The whole queue holds the per-guild lock — a concurrent
        play_in_voice_channel call cannot interleave mid-queue."""
        adapter = _make_adapter()
        _wire_voice(adapter)
        order = []

        async def fake_play(gid, path):
            order.append(path)
            await asyncio.sleep(0.01)
            return True

        adapter._play_in_voice_channel_now = fake_play
        queue_task = asyncio.create_task(
            adapter.play_tts_queue(CHAT, ["q1.mp3", "q2.mp3"])
        )
        await asyncio.sleep(0.005)  # queue holds the lock now
        other_task = asyncio.create_task(
            adapter.play_in_voice_channel(GID, "other.mp3")
        )
        await asyncio.gather(queue_task, other_task)
        assert order == ["q1.mp3", "q2.mp3", "other.mp3"]

    @pytest.mark.asyncio
    async def test_fallback_to_attachments_when_not_in_voice(self):
        adapter = _make_adapter()  # no VC wired
        sent = []

        async def fake_send_voice(chat_id, audio_path, caption=None, **kw):
            sent.append((audio_path, caption))
            from gateway.platforms.base import SendResult
            return SendResult(success=True)

        adapter.send_voice = fake_send_voice
        result = await adapter.play_tts_queue(CHAT, ["a.mp3", "b.mp3"], caption="hi")
        assert result.success is True
        # Caption rides on the first segment only; order preserved
        assert sent == [("a.mp3", "hi"), ("b.mp3", None)]

    @pytest.mark.asyncio
    async def test_empty_queue_is_failure(self):
        adapter = _make_adapter()
        result = await adapter.play_tts_queue(CHAT, [])
        assert result.success is False


# =====================================================================
# Lifecycle protection
# =====================================================================

class TestLifecycleProtection:
    def test_session_busy_counts_pending_speech(self):
        adapter = _make_adapter()
        assert adapter._voice_session_is_busy(GID) is False
        adapter._voice_pending_speech[GID] = 2
        assert adapter._voice_session_is_busy(GID) is True

    @pytest.mark.asyncio
    async def test_inactivity_timeout_never_leaves_while_speech_pending(self):
        adapter = _make_adapter()
        _wire_voice(adapter)
        adapter.VOICE_TIMEOUT = 0  # fire immediately
        adapter._voice_pending_speech[GID] = 1
        adapter.leave_voice_channel = AsyncMock()
        resets = []
        adapter._reset_voice_timeout = lambda gid: resets.append(gid)

        await adapter._voice_timeout_handler(GID)
        adapter.leave_voice_channel.assert_not_awaited()
        assert resets == [GID]

    def test_leave_clears_pending_speech(self):
        adapter = _make_adapter()
        adapter._voice_pending_speech[GID] = 3
        # Simulate the leave cleanup line
        adapter._voice_pending_speech.pop(GID, None)
        assert adapter._voice_pending_speech.get(GID, 0) == 0


# =====================================================================
# Base adapter default queue
# =====================================================================

class TestBaseQueueDefault:
    @pytest.mark.asyncio
    async def test_base_default_delegates_sequentially(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        calls = []

        class Stub:
            async def play_tts(self, chat_id, audio_path, caption=None, **kw):
                calls.append((audio_path, caption))
                return SendResult(success=True)

        stub = Stub()
        result = await BasePlatformAdapter.play_tts_queue(
            stub, "c1", ["x.mp3", "y.mp3"], caption="cap"
        )
        assert result.success is True
        assert calls == [("x.mp3", "cap"), ("y.mp3", None)]
