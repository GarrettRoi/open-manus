import asyncio
import logging
import os
import time
import wave
import tempfile
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

class VoiceSink:
    """Receives audio from a Discord voice channel and transcribes it."""
    
    def __init__(self, adapter, channel_id: str):
        self.adapter = adapter
        self.channel_id = channel_id
        self.audio_data: Dict[int, bytearray] = {}  # user_id -> audio bytes
        self.last_activity: Dict[int, float] = {}   # user_id -> timestamp
        self.is_processing: Dict[int, bool] = {}    # user_id -> processing flag
        self.silence_threshold = 1.5  # seconds of silence before processing
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        """Start the background task to monitor silence and process audio."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_silence())
        logger.info("VoiceSink started for channel %s", self.channel_id)

    def stop(self):
        """Stop the background task."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("VoiceSink stopped for channel %s", self.channel_id)

    def want_user(self, user):
        """Filter which users to listen to (ignore bots and self)."""
        if user.bot or user.id == self.adapter._client.user.id:
            return False
        # Only listen to the owner for now to prevent noise
        from gateway.platforms.discord import OWNER_USER_ID
        return str(user.id) == OWNER_USER_ID

    def write(self, user, data):
        """Callback for receiving audio packets."""
        user_id = user.id
        if user_id not in self.audio_data:
            self.audio_data[user_id] = bytearray()
            self.is_processing[user_id] = False
        
        # Don't append if we are currently transcribing previous chunk
        if not self.is_processing[user_id]:
            self.audio_data[user_id].extend(data.pcm)
            self.last_activity[user_id] = time.time()

    async def _monitor_silence(self):
        """Periodically check for users who have stopped speaking."""
        while self._running:
            try:
                await asyncio.sleep(0.5)
                now = time.time()
                for user_id, last_time in list(self.last_activity.items()):
                    if now - last_time > self.silence_threshold and len(self.audio_data.get(user_id, b"")) > 0:
                        if not self.is_processing.get(user_id, False):
                            # User has stopped speaking, process the audio
                            asyncio.create_task(self._process_user_audio(user_id))
            except Exception as e:
                logger.error("Error in VoiceSink monitor: %s", e)

    async def _process_user_audio(self, user_id: int):
        """Save audio to WAV, transcribe, and send to agent."""
        self.is_processing[user_id] = True
        try:
            data = self.audio_data[user_id]
            self.audio_data[user_id] = bytearray() # Clear buffer
            
            if len(data) < 16000: # Less than 0.5s of audio (at 32k/16bit/mono)
                self.is_processing[user_id] = False
                return

            # Save to temporary WAV file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp_path, "wb") as wav:
                    wav.setnchannels(2) # discord-ext-voice-recv provides stereo
                    wav.setsampwidth(2) # 16-bit
                    wav.setframerate(48000) # Discord standard
                    wav.writeframes(data)
            
            # Transcribe
            from skills.voice_stt.whisper_stt import transcribe_audio
            text = await asyncio.to_thread(transcribe_audio, tmp_path)
            
            # Cleanup
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            
            if text and len(text) > 1:
                logger.info("Voice Input from user %s: %s", user_id, text)
                # Create a fake message event and send to adapter
                user = await self.adapter._client.fetch_user(user_id)
                # We need a channel object to build the event
                channel = self.adapter._client.get_channel(int(self.channel_id))
                if not channel:
                    channel = await self.adapter._client.fetch_channel(int(self.channel_id))
                
                # Use a special prefix so the agent knows it came from voice
                voice_text = f"[VOICE INPUT] {text}"
                
                # Build the event manually or use a helper
                from gateway.platforms.base import MessageEvent, MessageType
                event = MessageEvent(
                    platform=self.adapter.platform,
                    channel_id=self.channel_id,
                    user_id=str(user_id),
                    username=user.display_name,
                    content=voice_text,
                    message_id=f"voice_{int(time.time())}",
                    message_type=MessageType.TEXT,
                    raw_message=None # No real message object
                )
                
                # Trigger the agent response
                await self.adapter.handle_message(event)
                
        except Exception as e:
            logger.error("Failed to process user audio: %s", e)
        finally:
            self.is_processing[user_id] = False
