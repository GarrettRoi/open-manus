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
        self.silence_threshold = float(os.getenv("VOICE_SILENCE_THRESHOLD", "1.2"))

        # Background monitor handle
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Allowed users (empty string = allow everyone who isn't a bot)
        self._allowed_users_raw = os.getenv("DISCORD_ALLOWED_USERS", "").strip()

        # Debug: count packets received so we can confirm write() is being called
        self._packet_count = 0
        self._first_packet_logged = False
        
        # Trace logging: periodically report packet counts per user to Discord
        self._last_debug_report = 0
        self._user_packet_counts: Dict[int, int] = {}

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
        
        # Aggressive debug: log every 100 packets to console
        if self._packet_count % 100 == 0:
            logger.debug("[VoiceSink] Received %d total packets", self._packet_count)

        if user:
            self._user_packet_counts[user.id] = self._user_packet_counts.get(user.id, 0) + 1

        # Log the very first packet so we know the pipeline is alive
        if not self._first_packet_logged:
            self._first_packet_logged = True
            user_info = f"user={user} (id={getattr(user, 'id', '?')})" if user else "user=None"
            # Access PCM data correctly based on library version
            pcm_data = getattr(data, 'pcm', data)
            pcm_len = len(pcm_data) if isinstance(pcm_data, (bytes, bytearray)) else 'UNKNOWN'
            
            logger.info("[VoiceSink] *** FIRST AUDIO PACKET RECEIVED *** %s, pcm_bytes=%s", user_info, pcm_len)
            asyncio.create_task(self._send_debug_message(f"📡 **Audio Stream Active**: Bot is now receiving audio packets from Discord!"))

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
            asyncio.create_task(self._send_debug_message(f"👤 **Speaker Detected**: Bot sees audio from <@{user_id}>"))

        # Don't append while we are transcribing the previous chunk
        if not self.is_processing.get(user_id, False):
            pcm_data = getattr(data, 'pcm', data)
            if isinstance(pcm_data, (bytes, bytearray)):
                self.audio_data[user_id].extend(pcm_data)
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
        while self._running:
            try:
                await asyncio.sleep(0.5)
                now = time.time()

                # Every 5 seconds, if we have seen packets but no transcription, report status
                if now - self._last_debug_report > 5.0 and self._packet_count > 0:
                    self._last_debug_report = now
                    report = f"📊 **Voice Traffic**: {self._packet_count} total packets."
                    has_data = False
                    for uid, count in self._user_packet_counts.items():
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

            # Minimum audio threshold: 0.1s = 19200 bytes
            if len(data) < 19_200:
                logger.info("[VoiceSink] Audio too short from user %s (%d bytes) — skipping", user_id, len(data))
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

            # Check that VOICE_TOOLS_OPENAI_KEY is set
            if not os.getenv("VOICE_TOOLS_OPENAI_KEY"):
                error_msg = "⚠️ **VOICE_TOOLS_OPENAI_KEY not set** — cannot transcribe audio."
                await self._send_debug_message(error_msg)
                self.is_processing[user_id] = False
                return

            # Transcribe
            result = await asyncio.to_thread(transcribe_audio, tmp_path)

            if not result.get("success"):
                error_detail = result.get("error", "unknown")
                await self._send_debug_message(f"⚠️ **Transcription failed**: {error_detail}")
                self.is_processing[user_id] = False
                return

            text = (result.get("transcript") or "").strip()
            if len(text) <= 1:
                await self._send_debug_message(f"🔇 Audio from <@{user_id}> was processed but no clear speech was found.")
                self.is_processing[user_id] = False
                return

            # Send visible confirmation
            await self._send_debug_message(f"🗣️ **Heard from** <@{user_id}>: \"{text}\"\n_Processing response..._")

            # Resolve the Discord user and channel objects
            user_obj = self.adapter._client.get_user(user_id)
            if user_obj is None:
                user_obj = await self.adapter._client.fetch_user(user_id)
            
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
                async with reply_channel.typing():
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
