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
import threading
import time
import traceback
import wave
import tempfile
from typing import Dict, List, Optional

try:
    import audioop  # stdlib (<=3.12) or audioop-lts shim (3.13+)
    _AUDIOOP_OK = True
except ImportError:
    audioop = None
    _AUDIOOP_OK = False

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

        # Transcripts of already-processed chunks belonging to the current
        # (still ongoing) utterance, per user.  Dispatched as one message when
        # the utterance ends (silence-triggered "final" flush).
        self.pending_transcripts: Dict[int, List[str]] = {}

        # Guards the buffer swap between the PacketRouter thread (write) and
        # the event loop (processing).  Without it bytes appended between
        # "snapshot" and "clear" would be silently lost.
        self._buffer_lock = threading.Lock()

        # Per-user asyncio locks so chunks are processed strictly in order.
        self._process_locks: Dict[int, asyncio.Lock] = {}

        # Silence detection: seconds of quiet before we process the buffer
        self.silence_threshold = float(os.getenv("VOICE_SILENCE_THRESHOLD", "1.5"))

        # Proactive chunking: flush the buffer once it holds this many seconds
        # of audio, well before the STT 25MB file cap.  60s of 48kHz stereo
        # 16-bit PCM is ~11.5MB raw, ~1.9MB after 16kHz-mono downsampling.
        self.max_chunk_seconds = float(os.getenv("VOICE_MAX_CHUNK_SECONDS", "60"))
        self.max_chunk_bytes = int(self.max_chunk_seconds * 48000 * 2 * 2)

        # Bounded retries for a failed chunk transcription
        self.transcribe_retries = int(os.getenv("VOICE_TRANSCRIBE_RETRIES", "2"))
        self.retry_delay = float(os.getenv("VOICE_TRANSCRIBE_RETRY_DELAY", "2"))

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
            with self._buffer_lock:
                if user_id not in self.audio_data:
                    self.audio_data[user_id] = bytearray()
            self.is_processing.setdefault(user_id, False)
            logger.info("[VoiceSink] New speaker detected: %s (id=%s)", getattr(user, 'display_name', user_id), user_id)
            self._schedule_async(self._send_debug_message(
                f"👤 **Speaker Detected**: Receiving audio from <@{user_id}>"
            ))

        # ALWAYS append — even while a previous chunk is being transcribed.
        # Audio arriving mid-transcription is buffered and processed as the
        # next chunk instead of being dropped.
        pcm_data = getattr(data, 'pcm', None)
        if pcm_data is None:
            pcm_data = data if isinstance(data, (bytes, bytearray)) else b""
        if isinstance(pcm_data, (bytes, bytearray)) and len(pcm_data) > 0:
            with self._buffer_lock:
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
        with self._buffer_lock:
            self.audio_data.clear()
        self.last_activity.clear()
        self.is_processing.clear()
        self.pending_transcripts.clear()
        self._process_locks.clear()
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
                        if self.is_processing.get(user_id, False):
                            continue
                        if len(buf) >= self.max_chunk_bytes:
                            # Proactive flush: buffer is approaching a size the
                            # STT layer can't safely handle in one upload.
                            logger.info("[VoiceSink] Buffer for user %s reached %d bytes (max %d) — flushing partial chunk",
                                        user_id, len(buf), self.max_chunk_bytes)
                            self.is_processing[user_id] = True
                            asyncio.create_task(self._process_user_audio(user_id, final=False))
                        elif silence_duration > self.silence_threshold and (
                                len(buf) > 0 or self.pending_transcripts.get(user_id)):
                            logger.info("[VoiceSink] Silence detected for user %s (%.1fs silence, %d bytes buffered) — triggering transcription",
                                        user_id, silence_duration, len(buf))
                            self.is_processing[user_id] = True
                            asyncio.create_task(self._process_user_audio(user_id, final=True))
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

    def _snapshot_buffer(self, user_id: int, limit: Optional[int] = None) -> bytes:
        """Atomically take up to ``limit`` bytes from the buffer.

        Uses the buffer lock so packets appended concurrently from the
        PacketRouter thread are never lost between snapshot and clear.
        The limit is frame-aligned (4 bytes per 48kHz stereo 16-bit frame).
        """
        with self._buffer_lock:
            buf = self.audio_data.get(user_id)
            if not buf:
                return b""
            if limit is None or len(buf) <= limit:
                data = bytes(buf)
                self.audio_data[user_id] = bytearray()
            else:
                cut = limit - (limit % 4)  # keep frame alignment
                data = bytes(buf[:cut])
                self.audio_data[user_id] = bytearray(buf[cut:])
            return data

    @staticmethod
    def _prepare_wav(data: bytes, tmp_path: str) -> float:
        """Write PCM to a WAV file, downsampled to 16kHz mono when possible.

        Downsampling shrinks the upload ~6x (192KB/s -> 32KB/s), keeping
        long chunks far under the STT 25MB cap and avoiding upload timeouts.
        Returns the audio duration in seconds.
        """
        duration_secs = len(data) / (48000 * 2 * 2)
        if _AUDIOOP_OK:
            try:
                mono = audioop.tomono(data, 2, 0.5, 0.5)
                downsampled, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
                with wave.open(tmp_path, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(downsampled)
                return duration_secs
            except Exception as e:
                logger.warning("[VoiceSink] Downsampling failed (%s) — falling back to raw 48kHz stereo", e)
        with wave.open(tmp_path, "wb") as wav:
            wav.setnchannels(2)       # discord-ext-voice-recv: stereo
            wav.setsampwidth(2)       # 16-bit
            wav.setframerate(48000)   # Discord standard
            wav.writeframes(data)
        return duration_secs

    async def _transcribe_with_retry(self, tmp_path: str) -> dict:
        """Transcribe a WAV file with bounded retries. The source audio file
        is kept on disk until this returns, so a transient failure never
        destroys the chunk."""
        transcribe_fn = _get_transcribe_fn()
        attempts = 1 + max(0, self.transcribe_retries)
        result: dict = {"success": False, "transcript": "", "error": "not attempted"}
        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(transcribe_fn, tmp_path)
            except Exception as e:
                # Treat a raising transcriber (network/client error) as a
                # failed attempt so bounded retry still applies.
                result = {"success": False, "transcript": "",
                          "error": f"{type(e).__name__}: {e}"}
            if result.get("success"):
                return result
            logger.warning("[VoiceSink] Transcription attempt %d/%d failed: %s",
                           attempt, attempts, result.get("error", "unknown"))
            if attempt < attempts:
                await asyncio.sleep(min(self.retry_delay * attempt, 8))
        return result

    async def _process_user_audio(self, user_id: int, final: bool = True):
        """Transcribe the current chunk for a user.

        ``final=False`` means this is a proactive size-based flush mid-utterance:
        the transcript is stored in ``pending_transcripts`` and dispatch waits
        for the utterance to end.  ``final=True`` (silence detected) stitches
        all pending chunk transcripts plus this one into a single message.
        """
        self.is_processing[user_id] = True
        lock = self._process_locks.setdefault(user_id, asyncio.Lock())
        try:
          async with lock:
            # Minimum audio threshold: ~0.25s at 48kHz stereo 16-bit = 48000 bytes
            # (48000 samples/sec * 2 channels * 2 bytes * 0.25s = 48000)
            MIN_AUDIO_BYTES = 48_000

            # Collect the chunk(s) to transcribe.  A partial (size-based)
            # flush takes exactly one capped chunk; a final flush drains the
            # whole buffer in capped chunks so even a huge backlog stays
            # under the STT file-size limit.
            chunks = []
            if final:
                while True:
                    piece = self._snapshot_buffer(user_id, self.max_chunk_bytes)
                    if not piece:
                        break
                    chunks.append(piece)
                    if len(piece) < self.max_chunk_bytes:
                        break
                # A short tail after a cap-sized chunk is real speech — merge
                # it into the previous chunk instead of discarding it (the
                # cap has ample headroom under the STT size limit).
                if len(chunks) >= 2 and len(chunks[-1]) < MIN_AUDIO_BYTES:
                    tail = chunks.pop()
                    chunks[-1] = chunks[-1] + tail
            else:
                piece = self._snapshot_buffer(user_id, self.max_chunk_bytes)
                if piece:
                    chunks.append(piece)

            pending = self.pending_transcripts.get(user_id) or []
            total_bytes = sum(len(c) for c in chunks)
            if final and chunks and pending and total_bytes < MIN_AUDIO_BYTES:
                # Sub-minimum tail of a longer utterance (earlier chunks were
                # already flushed) — pad with silence so it is transcribed
                # instead of discarded.
                chunks[-1] = chunks[-1] + b"\x00" * (MIN_AUDIO_BYTES - total_bytes)
                total_bytes = MIN_AUDIO_BYTES
            if total_bytes < MIN_AUDIO_BYTES and not (final and pending):
                logger.info("[VoiceSink] Audio too short from user %s (%d bytes, need %d) — skipping",
                            user_id, total_bytes, MIN_AUDIO_BYTES)
                return

            if chunks and total_bytes >= MIN_AUDIO_BYTES:
                # Check that VOICE_TOOLS_OPENAI_KEY is set
                if not os.getenv("VOICE_TOOLS_OPENAI_KEY"):
                    await self._send_debug_message(
                        "⚠️ **VOICE_TOOLS_OPENAI_KEY not set** — cannot transcribe audio."
                    )
                    return

                # PCM format: 48kHz, stereo (2ch), 16-bit (2 bytes) = 192000 bytes/sec
                duration_secs = total_bytes / (48000 * 2 * 2)
                logger.info("[VoiceSink] Processing %d bytes (%.1fs) of audio from user %s in %d chunk(s) (final=%s)",
                            total_bytes, duration_secs, user_id, len(chunks), final)
                await self._send_debug_message(
                    f"🎤 **Voice detected** from <@{user_id}> — captured {duration_secs:.1f}s of audio. Transcribing..."
                )

                for data in chunks:
                    chunk_secs = len(data) / (48000 * 2 * 2)
                    tmp_path = None
                    try:
                        # Write a WAV (16kHz mono when possible) for the STT provider
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                            tmp_path = tmp.name
                        self._prepare_wav(data, tmp_path)

                        result = await self._transcribe_with_retry(tmp_path)
                    finally:
                        if tmp_path:
                            try:
                                os.remove(tmp_path)
                            except OSError:
                                pass

                    if result.get("success"):
                        chunk_text = (result.get("transcript") or "").strip()
                        if chunk_text:
                            self.pending_transcripts.setdefault(user_id, []).append(chunk_text)
                    else:
                        error_detail = result.get("error", "unknown")
                        await self._send_debug_message(
                            f"⚠️ **Transcription failed** for {chunk_secs:.1f}s of audio from <@{user_id}> "
                            f"after {1 + max(0, self.transcribe_retries)} attempts: {error_detail}"
                        )
                        # Keep going: deliver whatever chunks succeeded.

            if not final:
                # Mid-utterance chunk: transcript stashed, dispatch waits for
                # the utterance to end (silence-triggered final flush).
                return

            # Final flush: stitch chunk transcripts in order.
            parts = self.pending_transcripts.pop(user_id, [])
            text = " ".join(p for p in parts if p).strip()

            if len(text) <= 1:
                await self._send_debug_message(
                    f"🔇 Audio from <@{user_id}> was processed but no clear speech was found."
                )
                return

            # Send visible confirmation
            display_text = text if len(text) <= 1500 else text[:1500] + "…"
            await self._send_debug_message(
                f"🗣️ **Heard from** <@{user_id}>: \"{display_text}\"\n_Processing response..._"
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
            self.is_processing[user_id] = False
