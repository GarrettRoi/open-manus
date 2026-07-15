import asyncio
import logging
import os
import queue
from typing import Callable, Optional

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, AudioInterface

logger = logging.getLogger(__name__)

class DiscordAudioInterface(AudioInterface):
    """
    Custom AudioInterface for ElevenLabs Conversational AI that bridges with Discord.
    """
    def __init__(self, on_audio_chunk: Callable[[bytes], None]):
        self.on_audio_chunk = on_audio_chunk
        self.input_queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

    def interrupt(self):
        """Handle agent interruption (e.g., stop playing current audio)."""
        logger.info("ElevenLabs: Interruption requested")
        # In a more advanced implementation, we would clear the Discord voice queue here

    def output_audio(self, audio: bytes):
        """Receive audio from ElevenLabs and send it to Discord."""
        # ElevenLabs sends audio in chunks. We pass it to the callback which puts it in the Discord queue.
        self.on_audio_chunk(audio)

    def input_audio(self) -> bytes:
        """
        This method is expected to block and return audio bytes for ElevenLabs.
        However, ElevenLabs SDK also supports pushing audio.
        If we must implement this, we'll use a thread-safe queue.
        """
        # The SDK's Conversation class usually calls this in a loop in a separate thread.
        # We'll use a sync queue to bridge from our async voice_sink.
        if not hasattr(self, '_sync_input_queue'):
            self._sync_input_queue = queue.Queue()
        
        return self._sync_input_queue.get()

    def push_audio(self, pcm_data: bytes):
        """Push PCM data from Discord VoiceSink into the input queue."""
        if not hasattr(self, '_sync_input_queue'):
            self._sync_input_queue = queue.Queue()
        self._sync_input_queue.put(pcm_data)

class ElevenLabsConversationalManager:
    """
    Manages ElevenLabs Conversational AI sessions for Discord agents.
    """
    def __init__(self, agent_id: str, api_key: Optional[str] = None):
        self.agent_id = agent_id
        self.client = ElevenLabs(api_key=api_key)
        self.conversation: Optional[Conversation] = None
        self.audio_interface: Optional[DiscordAudioInterface] = None

    async def start_session(self, on_audio_chunk: Callable[[bytes], None]):
        """Start a new conversational session."""
        self.audio_interface = DiscordAudioInterface(on_audio_chunk)
        self.conversation = Conversation(
            client=self.client,
            agent_id=self.agent_id,
            audio_interface=self.audio_interface,
            callback_agent_response=lambda text: logger.info(f"ElevenLabs Agent: {text}"),
            callback_agent_response_correction=lambda original, corrected: logger.info(f"ElevenLabs Correction: {original} -> {corrected}"),
            callback_user_transcript=lambda text: logger.info(f"ElevenLabs User: {text}"),
        )
        
        # Start the conversation in a background thread as per SDK design
        self.conversation.start_session()
        logger.info(f"ElevenLabs session started for agent {self.agent_id}")

    def stop_session(self):
        """End the current session."""
        if self.conversation:
            self.conversation.end_session()
            self.conversation = None
            logger.info("ElevenLabs session ended")

    def push_audio(self, pcm_data: bytes):
        """Forward audio from Discord to ElevenLabs."""
        if self.audio_interface:
            self.audio_interface.push_audio(pcm_data)
