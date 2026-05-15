"""
VoiceSink — Real-time voice receiving for Discord agents.

Extends discord-ext-voice-recv's AudioSink to capture per-user PCM audio,
detect silence boundaries, transcribe via Whisper, and route the resulting
text into the agent's normal message pipeline.

Requirements:
  - discord-ext-voice-recv (pip install discord-ext-voice-recv)
  - PyNaCl (pip install PyNaCl)
  - VOICE_TOOLS_OPENAI_KEY set in environment (for Whisper transcription)
"""

import asyncio
import logging
import os
import time
import traceback
import wave
import tempfile
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import the voice_recv AudioSink base class.  If the package is missing we
# fall back to a plain object so the module can still be imported (the
# adapter guards usage behind VOICE_RECV_AVAILABLE).
# ---------------------------------------------------------------------------
try:
    from discord.ext import voice_recv
    _AudioSinkBase = voice_recv.AudioSink
    _VOICE_RECV_OK = True
except ImportError:
    _AudioSinkBase = object
    _VOICE_RECV_OK = False
    logger.warning("discord-ext-voice-recv not installed — VoiceSink will be non-functional")

# ---------------------------------------------------------------------------
# Import transcription_tools directly to avoid triggering tools/__init__.py
# which eagerly imports firecrawl and other heavy dependencies.
# ---------------------------------------------------------------------------
_transcribe_audio = None

def _get_transcribe_fn():
    """Lazy-load the transcribe_audio function to avoid import-time failures."""
    global _transcribe_audio
    if _transcribe_audio is not None:
        return _transcribe_audio

    try:
        # Try the normal import path first (works when all deps are installed)
        from tools.transcription_tools import transcribe_audio
        _transcribe_audio = transcribe_audio
    except ImportError:
        # Fallback: import the module directly without going through __init__
        import importlib.util
        import pathlib
        module_path = pathlib.Path(__file__).resolve().parents[2] / "tools" / "transcription_tools.py"
        if module_path.exists():
            spec = importlib.util.spec_from_file_location("transcription_tools", str(module_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _transcribe_audio = mod.transcribe_audio
        else:
            logger.error("[VoiceSink] Cannot find transcription_tools.py at %s", module_path)
            # Return a stub that always fails gracefully
            def _stub(file_path, model=None):
                return {"success": False, "transcript": "", "error": "transcription_tools not available"}
            _transcribe_audio = _stub

    return _transcribe_audio


# Import base types (these are lightweight and should always work)
from gateway.platforms.base import MessageEvent, MessageType


class VoiceSink(_AudioSinkBase):
    """Receives decoded PCM audio from Discord and transcribes it using Whisper."""

    def __init__(self, adapter, channel_id: str):
        if _VOICE_RECV_OK:
            super().__init__()
        self.adapter = adapter
        self.channel_id = channel_id

        # Per-user audio buffers
        self.audio_data: Dict[int, bytearray] = {}
        self.last_activity: Dict[int, float] = {}
        self.is_processing: Dict[int, bool] = {}

        # Silence detection: seconds of quiet before we process the buffer
        self.silence_threshold = float(os.getenv("VOICE_SILENCE_THRESHOLD", "1.5"))

        # Background monitor handle
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stopped_intentionally = False  # Track if stop() was called by us vs cleanup

        # Allowed users (empty string = allow everyone who isn't a bot)
        self._allowed_users_raw = os.getenv("DISCORD_ALLOWED_USERS", "").strip()

        # Debug: count packets received so we can confirm write() is being called
        self._packet_count = 0
        self._first_packet_logged = False

        # Trace logging: periodically report packet counts per user to Discord
        self._last_debug_report = 0
        self._user_packet_counts: Dict[int, int] = {}

        # Error tracking
        self._write_errors = 0
        self._last_error = ""

        # Event loop reference (write() is called from a non-async thread)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        logger.info("[VoiceSink] Initialized for channel %s (silence_threshold=%.1fs, allowed_users='%s')",
                     channel_id, self.silence_threshold, self._allowed_users_raw)

    # ------------------------------------------------------------------
    # AudioSink required interface
    # ------------------------------------------------------------------

    def wants_opus(self) -> bool:
        """We want decoded PCM, not raw Opus packets."""
        return False

    def write(self, user, data):
        """Called by discord-ext-voice-recv for every decoded audio packet.

        CRITICAL: This method is called from the PacketRouter thread.
        If ANY exception propagates out of this method, the PacketRouter
        thread dies, which triggers stop_listening() -> cleanup() -> stop(),
        killing the entire voice pipeline. Therefore we MUST catch all
        exceptions here.
        """
        try:
            self._write_impl(user, data)
        except Exception as e:
            # NEVER let an exception escape write() — it would kill the router thread
            self._write_errors += 1
            self._last_error = f"{type(e).__name__}: {e}"
            if self._write_errors <= 5:
                logger.error("[VoiceSink] Exception in write() (error #%d): %s",
                             self._write_errors, e, exc_info=True)
            elif self._write_errors == 6:
                logger.error("[VoiceSink] Suppressing further write() error logs (too many errors)")

    def _write_impl(self, user, data):
        """Actual write implementation, separated so we can wrap it in try/except."""
        self._packet_count += 1

        # Aggressive debug: log every 500 packets to console
        if self._packet_count % 500 == 0:
            logger.info("[VoiceSink] Received %d total packets (%d errors so far)",
                        self._packet_count, self._write_errors)

        if user is not None:
            user_id_val = getattr(user, 'id', None)
            if user_id_val is not None:
                self._user_packet_counts[user_id_val] = self._user_packet_counts.get(user_id_val, 0) + 1

        # Log the very first packet so we know the pipeline is alive
        if not self._first_packet_logged:
            self._first_packet_logged = True
            user_info = f"user={user} (id={getattr(user, 'id', '?')})" if user else "user=None"
            # Access PCM data from VoiceData object
            pcm_data = getattr(data, 'pcm', None)
            if pcm_data is None:
                pcm_data = data if isinstance(data, (bytes, bytearray)) else b""
            pcm_len = len(pcm_data) if isinstance(pcm_data, (bytes, bytearray)) else 'UNKNOWN'

            logger.info("[VoiceSink] *** FIRST AUDIO PACKET *** %s, pcm_bytes=%s, data_type=%s",
                        user_info, pcm_len, type(data).__name__)
            self._schedule_async(self._send_debug_message(
                f"📡 **Audio Stream Active**: Receiving audio packets! First packet: {user_info}, {pcm_len} bytes"
            ))

        if user is None:
            return

        # Filter: ignore bots
        if getattr(user, "bot", False):
            return

        # Filter: ignore self
        try:
            if self.adapter._client and self.adapter._client.user and user.id == self.adapter._client.user.id:
                return
        except Exception:
            pass

        # If DISCORD_ALLOWED_USERS is set, enforce the allow-list
        if self._allowed_users_raw:
            allowed_ids = {u.strip() for u in self._allowed_users_raw.split(",") if u.strip()}
            if allowed_ids and str(user.id) not in allowed_ids:
                return

        user_id = user.id
        if user_id not in self.audio_data:
            self.audio_data[user_id] = bytearray()
            self.is_processing[user_id] = False
            logger.info("[VoiceSink] New speaker detected: %s (id=%s)", getattr(user, 'display_name', user_id), user_id)
            self._schedule_async(self._send_debug_message(
                f"👤 **Speaker Detected**: Receiving audio from <@{user_id}>"
            ))

        # Don't append while we are transcribing the previous chunk
        if not self.is_processing.get(user_id, False):
            # Extract PCM bytes from VoiceData object
            pcm_data = getattr(data, 'pcm', None)
            if pcm_data is None:
                pcm_data = data if isinstance(data, (bytes, bytearray)) else b""
            if isinstance(pcm_data, (bytes, bytearray)) and len(pcm_data) > 0:
                self.audio_data[user_id].extend(pcm_data)
                self.last_activity[user_id] = time.time()

    def cleanup(self):
        """Called by the library when listening stops.
        
        NOTE: This can be called by the library's __del__ or when the
        PacketRouter dies. We only want to fully stop if WE initiated it.
        """
        logger.info("[VoiceSink] cleanup() called — packets=%d, errors=%d, last_error='%s', intentional=%s",
                    self._packet_count, self._write_errors, self._last_error, self._stopped_intentionally)
        if not self._stopped_intentionally:
            # The library called cleanup on us (router died or GC collected something)
            # Log this prominently so we can debug
            logger.warning("[VoiceSink] cleanup() called unexpectedly (not by our stop()). "
                          "This usually means the PacketRouter thread died.")
            self._schedule_async(self._send_debug_message(
                f"⚠️ **Voice sink cleanup triggered unexpectedly!** "
                f"Packets received: {self._packet_count}, Errors: {self._write_errors}, "
                f"Last error: `{self._last_error or 'none'}`"
            ))
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    # ------------------------------------------------------------------
    # Thread-safe async scheduling
    # ------------------------------------------------------------------

    def _schedule_async(self, coro):
        """Schedule an async coroutine from a sync/threaded context."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            # Catch ALL exceptions, not just RuntimeError
            pass

    # ------------------------------------------------------------------
    # Background silence monitor
    # ------------------------------------------------------------------

    def start(self):
        """Start the background silence-detection task."""
        if self._running:
            return
        self._running = True
        self._stopped_intentionally = False
        # Capture the current event loop for thread-safe scheduling
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._monitor_silence())
        logger.info("[VoiceSink] Background silence monitor STARTED for channel %s (loop=%s)",
                    self.channel_id, self._loop)

    def stop(self):
        """Stop the background task and clear buffers."""
        self._stopped_intentionally = True
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.audio_data.clear()
        self.last_activity.clear()
        self.is_processing.clear()
        logger.info("[VoiceSink] STOPPED for channel %s", self.channel_id)

    async def _monitor_silence(self):
        """Periodically check for users who have stopped speaking."""
        logger.info("[VoiceSink] Silence monitor loop running...")
        try:
            while self._running:
                try:
                    await asyncio.sleep(0.3)
                    now = time.time()

                    # Every 10 seconds, if we have seen packets but no transcription, report status
                    if now - self._last_debug_report > 10.0 and self._packet_count > 0:
                        self._last_debug_report = now
                        report = (f"📊 **Voice Traffic**: {self._packet_count} total packets, "
                                  f"{self._write_errors} errors.")
                        has_data = False
                        for uid, count in list(self._user_packet_counts.items()):
                            buf_len = len(self.audio_data.get(uid, b""))
                            if count > 0 or buf_len > 0:
                                report += f"\n- <@{uid}>: {count} packets, {buf_len} bytes buffered."
                                has_data = True
                        if has_data:
                            await self._send_debug_message(report)

                    for user_id, last_time in list(self.last_activity.items()):
                        buf = self.audio_data.get(user_id, b"")
                        silence_duration = now - last_time
                        if silence_duration > self.silence_threshold and len(buf) > 0:
                            if not self.is_processing.get(user_id, False):
                                logger.info("[VoiceSink] Silence detected for user %s (%.1fs silence, %d bytes buffered) — triggering transcription",
                                            user_id, silence_duration, len(buf))
                                asyncio.create_task(self._process_user_audio(user_id))
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("[VoiceSink] Error in silence monitor: %s", e, exc_info=True)
                    await asyncio.sleep(1)  # Back off on error
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[VoiceSink] Silence monitor loop EXITED (running=%s)", self._running)

    # ------------------------------------------------------------------
    # Transcription + agent dispatch
    # ------------------------------------------------------------------

    async def _send_debug_message(self, text: str):
        """Send a visible debug message to the home channel so the user can see pipeline activity."""
        try:
            home_channel_id = os.getenv("DISCORD_HOME_CHANNEL", "")
            if not home_channel_id:
                return
            channel = self.adapter._client.get_channel(int(home_channel_id))
            if not channel:
                try:
                    channel = await self.adapter._client.fetch_channel(int(home_channel_id))
                except Exception:
                    return
            if channel:
                await channel.send(text)
        except Exception as e:
            logger.error("[VoiceSink] Failed to send debug message: %s", e)

    async def _process_user_audio(self, user_id: int):
        """Save buffered PCM to WAV, transcribe with Whisper, and dispatch."""
        self.is_processing[user_id] = True
        tmp_path = None
        try:
            data = bytes(self.audio_data[user_id])
            self.audio_data[user_id] = bytearray()  # clear buffer

            # Minimum audio threshold: ~0.25s at 48kHz stereo 16-bit = 48000 bytes
            # (48000 samples/sec * 2 channels * 2 bytes * 0.25s = 48000)
            MIN_AUDIO_BYTES = 48_000
            if len(data) < MIN_AUDIO_BYTES:
                logger.info("[VoiceSink] Audio too short from user %s (%d bytes, need %d) — skipping",
                            user_id, len(data), MIN_AUDIO_BYTES)
                self.is_processing[user_id] = False
                return

            logger.info("[VoiceSink] Processing %d bytes of audio from user %s", len(data), user_id)

            # Send a visible confirmation that audio was captured
            # PCM format: 48kHz, stereo (2ch), 16-bit (2 bytes) = 192000 bytes/sec
            duration_secs = len(data) / (48000 * 2 * 2)
            await self._send_debug_message(
                f"🎤 **Voice detected** from <@{user_id}> — captured {duration_secs:.1f}s of audio. Transcribing..."
            )

            # Write a proper WAV file for Whisper
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp_path, "wb") as wav:
                    wav.setnchannels(2)       # discord-ext-voice-recv: stereo
                    wav.setsampwidth(2)       # 16-bit
                    wav.setframerate(48000)   # Discord standard
                    wav.writeframes(data)

            # Check that VOICE_TOOLS_OPENAI_KEY is set
            if not os.getenv("VOICE_TOOLS_OPENAI_KEY"):
                error_msg = "⚠️ **VOICE_TOOLS_OPENAI_KEY not set** — cannot transcribe audio."
                await self._send_debug_message(error_msg)
                self.is_processing[user_id] = False
                return

            # Get the transcription function (lazy-loaded)
            transcribe_fn = _get_transcribe_fn()

            # Transcribe (run in thread to avoid blocking the event loop)
            result = await asyncio.to_thread(transcribe_fn, tmp_path)

            if not result.get("success"):
                error_detail = result.get("error", "unknown")
                await self._send_debug_message(f"⚠️ **Transcription failed**: {error_detail}")
                self.is_processing[user_id] = False
                return

            text = (result.get("transcript") or "").strip()
            if len(text) <= 1:
                await self._send_debug_message(
                    f"🔇 Audio from <@{user_id}> was processed but no clear speech was found."
                )
                self.is_processing[user_id] = False
                return

            # Send visible confirmation
            await self._send_debug_message(
                f"🗣️ **Heard from** <@{user_id}>: \"{text}\"\n_Processing response..._"
            )

            # Resolve the Discord user and channel objects
            user_obj = self.adapter._client.get_user(user_id)
            if user_obj is None:
                try:
                    user_obj = await self.adapter._client.fetch_user(user_id)
                except Exception:
                    pass

            user_display = user_obj.display_name if user_obj else str(user_id)
            home_channel_id = os.getenv("DISCORD_HOME_CHANNEL", self.channel_id)

            source = self.adapter.build_source(
                chat_id=home_channel_id,
                chat_name=user_display,
                chat_type="voice",
                user_id=str(user_id),
                user_name=user_display,
            )

            event = MessageEvent(
                text=f"[VOICE] {text}",
                message_type=MessageType.TEXT,
                source=source,
                raw_message=None,
                message_id=f"voice_{int(time.time())}_{user_id}",
                media_urls=[],
                media_types=[],
                reply_to_message_id=None,
                timestamp=None,
            )

            # Show typing indicator while the agent processes the voice input
            reply_channel = self.adapter._client.get_channel(int(home_channel_id))
            if reply_channel:
                try:
                    async with reply_channel.typing():
                        await self.adapter.handle_message(event)
                except Exception:
                    await self.adapter.handle_message(event)
            else:
                await self.adapter.handle_message(event)

        except Exception as e:
            logger.error("[VoiceSink] Failed to process user audio: %s", e, exc_info=True)
            await self._send_debug_message(f"⚠️ **Voice processing error**: {e}")
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            self.is_processing[user_id] = False
