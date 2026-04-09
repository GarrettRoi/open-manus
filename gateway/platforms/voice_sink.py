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
    """Receives decoded PCM audio from Discord and transcribes it using Whisper.

    Lifecycle:
        1. Created by the DiscordAdapter when joining a voice channel.
        2. Passed to ``voice_client.listen(sink)`` which calls ``write()``
           for every decoded audio packet.
        3. A background asyncio task (``_monitor_silence``) watches for
           silence gaps and triggers transcription + agent dispatch.
        4. ``cleanup()`` is called by the library when listening stops.
    """

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

    # ------------------------------------------------------------------
    # AudioSink required interface
    # ------------------------------------------------------------------

    def wants_opus(self) -> bool:
        """We want decoded PCM, not raw Opus packets."""
        return False

    def write(self, user, data):
        """Called by discord-ext-voice-recv for every decoded audio packet.

        Parameters
        ----------
        user : discord.Member | discord.User | None
            The user who produced this audio.  ``None`` when the speaker
            cannot be identified (rare).
        data : voice_recv.VoiceData
            Container with ``.pcm`` (bytes) for decoded audio.
        """
        if user is None:
            return

        # Filter: ignore bots and (optionally) non-allowed users
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

        # Don't append while we are transcribing the previous chunk
        if not self.is_processing.get(user_id, False):
            self.audio_data[user_id].extend(data.pcm)
            self.last_activity[user_id] = time.time()

    def cleanup(self):
        """Called by the library when listening stops."""
        self.stop()
        logger.info("VoiceSink cleanup for channel %s", self.channel_id)

    # ------------------------------------------------------------------
    # Background silence monitor
    # ------------------------------------------------------------------

    def start(self):
        """Start the background silence-detection task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_silence())
        logger.info("VoiceSink started for channel %s", self.channel_id)

    def stop(self):
        """Stop the background task and clear buffers."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.audio_data.clear()
        self.last_activity.clear()
        self.is_processing.clear()
        logger.info("VoiceSink stopped for channel %s", self.channel_id)

    async def _monitor_silence(self):
        """Periodically check for users who have stopped speaking."""
        while self._running:
            try:
                await asyncio.sleep(0.5)
                now = time.time()
                for user_id, last_time in list(self.last_activity.items()):
                    buf = self.audio_data.get(user_id, b"")
                    if now - last_time > self.silence_threshold and len(buf) > 0:
                        if not self.is_processing.get(user_id, False):
                            asyncio.create_task(self._process_user_audio(user_id))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in VoiceSink monitor: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Transcription + agent dispatch
    # ------------------------------------------------------------------

    async def _process_user_audio(self, user_id: int):
        """Save buffered PCM to WAV, transcribe with Whisper, and dispatch."""
        self.is_processing[user_id] = True
        tmp_path = None
        try:
            data = bytes(self.audio_data[user_id])
            self.audio_data[user_id] = bytearray()  # clear buffer

            # Minimum ~0.5 s of audio at 48 kHz / 16-bit / stereo = 96 000 bytes
            if len(data) < 96_000:
                logger.debug("Skipping short audio from user %s (%d bytes)", user_id, len(data))
                self.is_processing[user_id] = False
                return

            logger.info("Processing %d bytes of audio from user %s", len(data), user_id)

            # Write a proper WAV file for Whisper
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp_path, "wb") as wav:
                    wav.setnchannels(2)       # discord-ext-voice-recv: stereo
                    wav.setsampwidth(2)       # 16-bit
                    wav.setframerate(48000)   # Discord standard
                    wav.writeframes(data)

            # Transcribe (synchronous OpenAI call — run in thread)
            result = await asyncio.to_thread(transcribe_audio, tmp_path)

            if not result.get("success"):
                logger.warning("Transcription failed for user %s: %s", user_id, result.get("error", "unknown"))
                return

            text = (result.get("transcript") or "").strip()
            if len(text) <= 1:
                logger.debug("Transcription too short from user %s: '%s'", user_id, text)
                return

            logger.info("Voice transcription from user %s: %s", user_id, text)

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
            except Exception:
                await self.adapter.handle_message(event)

        except Exception as e:
            logger.error("Failed to process user audio: %s", e, exc_info=True)
        finally:
            # Clean up temp file
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            self.is_processing[user_id] = False
