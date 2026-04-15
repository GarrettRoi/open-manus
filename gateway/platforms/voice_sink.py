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

# Internal project imports
from tools.transcription_tools import transcribe_audio
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

        # Allowed users (empty string = allow everyone who isn't a bot)
        self._allowed_users_raw = os.getenv("DISCORD_ALLOWED_USERS", "").strip()

        # Debug: count packets received so we can confirm write() is being called
        self._packet_count = 0
        self._first_packet_logged = False

        logger.info("[VoiceSink] Initialized for channel %s (silence_threshold=%.1fs, allowed_users='%s')",
                     channel_id, self.silence_threshold, self._allowed_users_raw)

    # ------------------------------------------------------------------
    # AudioSink required interface
    # ------------------------------------------------------------------

    def wants_opus(self) -> bool:
        """We want decoded PCM, not raw Opus packets."""
        return False

    def write(self, user, data):
        """Called by discord-ext-voice-recv for every decoded audio packet."""
        self._packet_count += 1

        # Log the very first packet so we know the pipeline is alive
        if not self._first_packet_logged:
            self._first_packet_logged = True
            user_info = f"user={user} (id={getattr(user, 'id', '?')})" if user else "user=None"
            # In voice_recv, data is a VoiceData object, pcm is an attribute
            pcm_len = len(data.pcm) if hasattr(data, 'pcm') else 'NO_PCM_ATTR'
            logger.info("[VoiceSink] *** FIRST AUDIO PACKET RECEIVED *** %s, pcm_bytes=%s", user_info, pcm_len)

        # Log every 500 packets so we can see ongoing activity
        if self._packet_count % 500 == 0:
            logger.info("[VoiceSink] Received %d audio packets so far (buffers: %s)",
                        self._packet_count,
                        {uid: len(buf) for uid, buf in self.audio_data.items()})

        if user is None:
            return

        # Filter: ignore bots
        if getattr(user, "bot", False):
            return
        if self.adapter._client and self.adapter._client.user and user.id == self.adapter._client.user.id:
            return

        # If DISCORD_ALLOWED_USERS is set, enforce the allow-list
        if self._allowed_users_raw:
            allowed_ids = {u.strip() for u in self._allowed_users_raw.split(",") if u.strip()}
            if allowed_ids and str(user.id) not in allowed_ids:
                return

        user_id = user.id
        if user_id not in self.audio_data:
            self.audio_data[user_id] = bytearray()
            self.is_processing[user_id] = False
            logger.info("[VoiceSink] New speaker detected: %s (id=%s)", user.display_name, user_id)

        # Don't append while we are transcribing the previous chunk
        if not self.is_processing.get(user_id, False):
            if hasattr(data, 'pcm'):
                self.audio_data[user_id].extend(data.pcm)
                self.last_activity[user_id] = time.time()

    def cleanup(self):
        """Called by the library when listening stops."""
        self.stop()
        logger.info("[VoiceSink] cleanup called — total packets received: %d", self._packet_count)

    # ------------------------------------------------------------------
    # Background silence monitor
    # ------------------------------------------------------------------

    def start(self):
        """Start the background silence-detection task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_silence())
        logger.info("[VoiceSink] Background silence monitor STARTED for channel %s", self.channel_id)

    def stop(self):
        """Stop the background task and clear buffers."""
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
        loop_count = 0
        while self._running:
            try:
                await asyncio.sleep(0.5)
                loop_count += 1
                now = time.time()

                # Log monitor heartbeat every 60 iterations (~30 seconds)
                if loop_count % 60 == 0:
                    logger.info("[VoiceSink] Monitor heartbeat: loop=%d, packets=%d, buffers=%s",
                                loop_count, self._packet_count,
                                {uid: len(buf) for uid, buf in self.audio_data.items()})

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

    # ------------------------------------------------------------------
    # Transcription + agent dispatch
    # ------------------------------------------------------------------

    async def _send_debug_message(self, text: str):
        """Send a visible debug message to the home channel so the user can see pipeline activity."""
        try:
            home_channel_id = os.getenv("DISCORD_HOME_CHANNEL", "")
            if not home_channel_id:
                logger.warning("[VoiceSink] No DISCORD_HOME_CHANNEL set — cannot send debug message")
                return
            channel = self.adapter._client.get_channel(int(home_channel_id))
            if not channel:
                channel = await self.adapter._client.fetch_channel(int(home_channel_id))
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

            # Minimum ~0.5 s of audio at 48 kHz / 16-bit / stereo = 192 000 bytes
            # Note: 48000 samples/sec * 2 bytes/sample * 2 channels = 192000 bytes per second
            if len(data) < 96_000:
                logger.info("[VoiceSink] Audio too short from user %s (%d bytes < 96000) — skipping", user_id, len(data))
                self.is_processing[user_id] = False
                return

            logger.info("[VoiceSink] Processing %d bytes of audio from user %s", len(data), user_id)

            # Send a visible confirmation that audio was captured
            duration_secs = len(data) / (48000 * 2 * 2)  # 48kHz, 16-bit, stereo
            await self._send_debug_message(f"🎤 **Voice detected** from <@{user_id}> — captured {duration_secs:.1f}s of audio. Transcribing...")

            # Write a proper WAV file for Whisper
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp_path, "wb") as wav:
                    wav.setnchannels(2)       # discord-ext-voice-recv: stereo
                    wav.setsampwidth(2)       # 16-bit
                    wav.setframerate(48000)   # Discord standard
                    wav.writeframes(data)

            logger.info("[VoiceSink] WAV file written: %s (%d bytes)", tmp_path, os.path.getsize(tmp_path))

            # Check that VOICE_TOOLS_OPENAI_KEY is set
            if not os.getenv("VOICE_TOOLS_OPENAI_KEY"):
                error_msg = "⚠️ **VOICE_TOOLS_OPENAI_KEY not set** — cannot transcribe audio. Please set this env var."
                logger.error("[VoiceSink] %s", error_msg)
                await self._send_debug_message(error_msg)
                self.is_processing[user_id] = False
                return

            # Transcribe (synchronous OpenAI call — run in thread)
            result = await asyncio.to_thread(transcribe_audio, tmp_path)

            if not result.get("success"):
                error_detail = result.get("error", "unknown")
                logger.warning("[VoiceSink] Transcription failed for user %s: %s", user_id, error_detail)
                await self._send_debug_message(f"⚠️ **Transcription failed**: {error_detail}")
                self.is_processing[user_id] = False
                return

            text = (result.get("transcript") or "").strip()
            if len(text) <= 1:
                logger.info("[VoiceSink] Transcription too short from user %s: '%s'", user_id, text)
                # No debug message for empty/too short transcription to avoid spam
                self.is_processing[user_id] = False
                return

            logger.info("[VoiceSink] ✅ Transcription from user %s: \"%s\"", user_id, text)

            # Send visible confirmation
            await self._send_debug_message(f"🗣️ **Heard from** <@{user_id}>: \"{text}\"\n_Processing response..._")

            # Resolve the Discord user and channel objects
            try:
                user_obj = self.adapter._client.get_user(user_id)
                if user_obj is None:
                    user_obj = await self.adapter._client.fetch_user(user_id)
            except Exception:
                user_obj = None

            channel = self.adapter._client.get_channel(int(self.channel_id))
            if not channel:
                try:
                    channel = await self.adapter._client.fetch_channel(int(self.channel_id))
                except Exception:
                    channel = None

            user_display = user_obj.display_name if user_obj else str(user_id)

            # Build a descriptive chat name
            chat_name = user_display
            if channel and hasattr(channel, "guild") and channel.guild:
                ch_name = getattr(channel, "name", "Voice")
                chat_name = f"{channel.guild.name} / Voice: {ch_name}"

            # Use the home channel as the reply destination so the agent
            # sends its text response there (voice TTS goes to the VC).
            home_channel_id = os.getenv("DISCORD_HOME_CHANNEL", self.channel_id)

            source = self.adapter.build_source(
                chat_id=home_channel_id,
                chat_name=chat_name,
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
            try:
                reply_channel = self.adapter._client.get_channel(int(home_channel_id))
                if reply_channel:
                    async with reply_channel.typing():
                        await self.adapter.handle_message(event)
                else:
                    await self.adapter.handle_message(event)
            except Exception as dispatch_err:
                logger.error("[VoiceSink] Error dispatching to agent: %s", dispatch_err, exc_info=True)
                await self._send_debug_message(f"⚠️ **Error dispatching voice to agent**: {dispatch_err}")

        except Exception as e:
            logger.error("[VoiceSink] Failed to process user audio: %s", e, exc_info=True)
            await self._send_debug_message(f"⚠️ **Voice processing error**: {e}")
        finally:
            # Clean up temp file
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            self.is_processing[user_id] = False
