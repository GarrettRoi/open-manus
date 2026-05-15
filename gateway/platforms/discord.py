"""
Discord platform adapter.

Uses discord.py library for:
- Receiving messages from servers and DMs
- Sending responses back
- Handling threads and channels
- Turn-based inter-agent communication protocol
"""

import asyncio
import logging
import os
import time
import io
import re
import wave
import tempfile
import shutil
from typing import Dict, List, Optional, Any, Set

logger = logging.getLogger(__name__)

try:
    import discord
    from discord import Message as DiscordMessage, Intents
    from discord.ext import commands
    try:
        from discord.ext import voice_recv
        VOICE_RECV_AVAILABLE = True
        logger.info("discord-ext-voice-recv is available — voice receiving enabled")
    except ImportError:
        voice_recv = None
        VOICE_RECV_AVAILABLE = False
        logger.warning("discord-ext-voice-recv not installed — voice receiving disabled. Install with: pip install discord-ext-voice-recv")
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None
    DiscordMessage = Any
    Intents = Any
    commands = None

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_url,
    cache_audio_from_url,
)

# ---------------------------------------------------------------------------
# Voice Streaming & Sanitization
# ---------------------------------------------------------------------------

def sanitize_for_voice(text: str) -> str:
    """Clean text for natural speech by removing technical jargon, code, and URLs."""
    if not text:
        return ""
    # Remove code blocks
    text = re.sub(r'```.*?```', '[code omitted]', text, flags=re.DOTALL)
    # Remove inline code
    text = re.sub(r'`.*?`', '', text)
    # Remove protocol tags
    text = re.sub(r'\[(NOTIFY|REQUEST|END|REPORT|GROUP CONVERSATION CONTEXT|LATEST MESSAGE FROM .*?)\]', '', text, flags=re.IGNORECASE)
    # Simplify URLs
    text = re.sub(r'https?://\S+', 'a link', text)
    # Clean up whitespace
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text



def check_discord_requirements() -> bool:
    """Check if Discord dependencies are available."""
    return DISCORD_AVAILABLE

def _has_ffmpeg() -> bool:
    """Check if FFmpeg is installed and available in the system path."""
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# Channel-Based Routing & Communication Protocol
# ---------------------------------------------------------------------------
# Architecture:
#   - harmony-communication: "War room" where Harmony delegates tasks via @mentions
#   - Agent home channels: Where agents do their actual work
#   - task-board: Harmony posts/updates task tracking items
#
# Message tags that control inter-agent behavior:
#   [NOTIFY]  - Notification only. Agent reacts but does NOT reply.
#   [REQUEST] - Expects exactly one response from the mentioned agent.
#   [END]     - Agent signals task is complete. No further replies.
#   [REPORT]  - Like REQUEST but response goes to the original channel.
#
# Mention rules (enforced programmatically):
#   - Only Harmony can @mention specific worker agents
#   - Worker agents can only @mention Harmony (not each other)
#   - Garrett (owner) always bypasses all restrictions
#
# Anti-doom-loop rules:
#   1. One response per bot message - tracked via cooldown set
#   2. No chain reactions - bot replies to bot messages don't trigger
#      unless the reply explicitly @mentions with [REQUEST]
#   3. [END] and [NOTIFY] messages never trigger replies
# ---------------------------------------------------------------------------

# Garrett's Discord user ID - always bypasses bot restrictions
OWNER_USER_ID = os.getenv("DISCORD_OWNER_ID", "700339484507766826")

# Harmony's Discord bot user ID - the only agent allowed to @mention others
HARMONY_BOT_ID = os.getenv("HARMONY_BOT_ID", "1481029359757299922")

# Channel IDs for routing
HARMONY_CHANNEL_ID = os.getenv("HARMONY_CHANNEL_ID", "1483488835928064110")  # harmony-communication
TASK_BOARD_CHANNEL_ID = os.getenv("TASK_BOARD_CHANNEL_ID", "1481406227153031372")  # task-board channel

# Cooldown window: ignore repeated triggers from same bot message (seconds)
BOT_RESPONSE_COOLDOWN = 120

# Group chat conversation timeout (seconds of inactivity before auto-close)
GROUP_CHAT_TIMEOUT = 600  # 10 minutes

# Delay before an agent responds in group chat (seconds)
GROUP_CHAT_RESPONSE_DELAY = 4


class GroupChatSession:
    """Tracks active group chat conversations where multiple agents can talk.
    
    Activated when the owner @mentions 2+ bots in one message.
    While active, agents in the session can @mention each other.
    Killed by 'end chat' from the owner or by timeout.
    """

    def __init__(self):
        self._sessions: Dict[str, Dict] = {}  # channel_id -> session info

    def activate(self, channel_id: str, participant_bot_ids: Set[str], trigger_message_id: str) -> None:
        """Start a group chat session in a channel."""
        self._sessions[channel_id] = {
            'participants': participant_bot_ids,
            'trigger_message_id': trigger_message_id,
            'last_activity': time.time(),
            'started_at': time.time(),
        }
        logger.info("Group chat activated in channel %s with %d participants", channel_id, len(participant_bot_ids))

    def deactivate(self, channel_id: str) -> None:
        """End a group chat session."""
        if channel_id in self._sessions:
            del self._sessions[channel_id]
            logger.info("Group chat deactivated in channel %s", channel_id)

    def is_active(self, channel_id: str) -> bool:
        """Check if a group chat is active in this channel (and not timed out)."""
        if channel_id not in self._sessions:
            return False
        session = self._sessions[channel_id]
        if time.time() - session['last_activity'] > GROUP_CHAT_TIMEOUT:
            self.deactivate(channel_id)
            return False
        return True

    def is_participant(self, channel_id: str, bot_id: str) -> bool:
        """Check if a bot is a participant in the active group chat."""
        if not self.is_active(channel_id):
            return False
        return bot_id in self._sessions[channel_id]['participants']

    def touch(self, channel_id: str) -> None:
        """Update last activity timestamp."""
        if channel_id in self._sessions:
            self._sessions[channel_id]['last_activity'] = time.time()

    def get_participants(self, channel_id: str) -> Set[str]:
        """Get participant bot IDs for a session."""
        if channel_id in self._sessions:
            return self._sessions[channel_id]['participants']
        return set()


class BotMessageTracker:
    """Tracks which bot messages this agent has already responded to,
    preventing doom loops and enforcing one-response-per-trigger."""

    def __init__(self):
        self._responded_to: Dict[str, float] = {}  # message_id -> timestamp
        self._cleanup_interval = 300  # Clean old entries every 5 min
        self._last_cleanup = time.time()

    def has_responded(self, message_id: str) -> bool:
        """Check if we already responded to this message."""
        self._maybe_cleanup()
        return message_id in self._responded_to

    def mark_responded(self, message_id: str) -> None:
        """Mark a message as responded to."""
        self._responded_to[message_id] = time.time()

    def _maybe_cleanup(self) -> None:
        """Remove entries older than cooldown to prevent memory leak."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - BOT_RESPONSE_COOLDOWN * 2
        self._responded_to = {
            mid: ts for mid, ts in self._responded_to.items() if ts > cutoff
        }


def parse_message_tag(content: str) -> Optional[str]:
    """Extract a protocol tag from message content.
    Returns 'NOTIFY', 'REQUEST', 'END', 'REPORT', or None."""
    upper = content.upper()
    for tag in ('NOTIFY', 'END', 'REQUEST', 'REPORT'):
        if f'[{tag}]' in upper:
            return tag
    return None


def strip_message_tags(content: str) -> str:
    """Remove protocol tags from message content before passing to LLM."""
    import re
    return re.sub(r'\[(NOTIFY|REQUEST|END|REPORT)\]', '', content, flags=re.IGNORECASE).strip()


# Per-agent signature emoji sets: (default_thinking, default_done, context_rules)
# context_rules: list of (keywords, thinking_emoji, done_emoji)
_AGENT_EMOJIS = {
    'harmony': (
        '💋', '👑',
        [(['delegate', 'assign', 'team'], '💅', '👑'),
         (['plan', 'strategy', 'goal'], '🧠', '💎'),
         (['urgent', 'asap', 'now'], '⚡', '🔥'),
         (['good', 'great', 'nice', 'thanks'], '😘', '💖')]
    ),
    'samantha': (
        '📋', '💅',
        [(['schedule', 'calendar', 'meeting', 'appointment'], '⏰', '💋'),
         (['email', 'message', 'send'], '💌', '😘'),
         (['document', 'file', 'report'], '📝', '✨'),
         (['remind', 'todo', 'task'], '🫦', '💅')]
    ),
    'sasha': (
        '🎯', '💰',
        [(['lead', 'prospect', 'client'], '👀', '🤑'),
         (['close', 'deal', 'sale', 'sold', 'book'], '🔥', '💰'),
         (['follow', 'touch', 'nurture', 'drip'], '💋', '😏'),
         (['convert', 'funnel', 'pipeline'], '⚡', '💎'),
         (['objection', 'concern', 'hesitant'], '😏', '🫦')]
    ),
    'scarlett': (
        '💝', '🥰',
        [(['happy', 'satisfied', 'love', 'thank'], '🥰', '💖'),
         (['issue', 'problem', 'complaint', 'upset'], '🫂', '💋'),
         (['review', 'feedback', 'testimonial'], '✨', '😍'),
         (['retain', 'loyalty', 'relationship'], '💕', '👑')]
    ),
    'bianca': (
        '📊', '💎',
        [(['stock', 'trade', 'buy', 'sell'], '📈', '🤑'),
         (['crypto', 'bitcoin', 'eth'], '⚡', '💰'),
         (['portfolio', 'invest', 'dividend'], '🧐', '💎'),
         (['risk', 'loss', 'drop', 'crash'], '😬', '🛡️'),
         (['profit', 'gain', 'up', 'moon'], '🔥', '🚀')]
    ),
    'valentina': (
        '⚙️', '🔥',
        [(['automate', 'workflow', 'n8n', 'automation'], '🤖', '⚡'),
         (['deploy', 'railway', 'server'], '🚀', '💅'),
         (['code', 'build', 'develop', 'fix', 'bug'], '👩‍💻', '🔥'),
         (['api', 'integration', 'connect'], '🔌', '💋')]
    ),
    'jade': (
        '🎵', '💃',
        [(['dj', 'music', 'song', 'playlist'], '🎶', '🔥'),
         (['wedding', 'event', 'reception', 'ceremony'], '💒', '💋'),
         (['booking', 'gig', 'book'], '📅', '🤑'),
         (['photo', 'booth'], '📸', '✨'),
         (['vows', 'vinyl'], '🎧', '💃')]
    ),
    'tatiana': (
        '🏠', '💋',
        [(['house', 'home', 'property', 'listing'], '🏡', '😍'),
         (['buyer', 'first time', 'purchase'], '🔑', '🥂'),
         (['closing', 'contract', 'offer', 'transaction'], '📝', '💰'),
         (['webinar', 'seminar', 'class'], '🎓', '🔥'),
         (['market', 'price', 'value'], '📊', '💎')]
    ),
    'sabrina': (
        '📱', '💅',
        [(['post', 'content', 'publish'], '✍️', '🔥'),
         (['follower', 'growth', 'audience', 'reach'], '📈', '👑'),
         (['instagram', 'facebook', 'tiktok', 'social'], '📲', '💋'),
         (['engage', 'comment', 'like', 'share'], '💬', '😘'),
         (['viral', 'trending', 'popular'], '⚡', '🌟')]
    ),
    'addison': (
        '📢', '🎯',
        [(['ad', 'campaign', 'advertis'], '🎯', '🔥'),
         (['roas', 'roi', 'convert', 'click'], '📊', '🤑'),
         (['audience', 'target', 'segment'], '👀', '💋'),
         (['budget', 'spend', 'cost'], '💸', '💰'),
         (['creative', 'copy', 'headline'], '✨', '💅')]
    ),
    'cora': (
        '🎨', '✨',
        [(['video', 'film', 'edit', 'footage'], '🎬', '🔥'),
         (['photo', 'image', 'graphic', 'design'], '📸', '💋'),
         (['brand', 'logo', 'visual'], '🎨', '💎'),
         (['content', 'create', 'produce'], '✍️', '👑'),
         (['audio', 'voice', 'sound', 'music'], '🎙️', '💅')]
    ),
    'raven': (
        '🔍', '🧠',
        [(['research', 'find', 'discover', 'look into'], '🕵️', '💎'),
         (['data', 'analysis', 'report', 'number'], '📊', '🔥'),
         (['competitor', 'market', 'trend'], '👀', '⚡'),
         (['source', 'article', 'study'], '📚', '💅'),
         (['insight', 'opportunity', 'intel'], '🧐', '💋')]
    ),
    'lexi': (
        '📚', '⚡',
        [(['memory', 'lesson', 'knowledge', 'learn'], '🧠', '💎'),
         (['skill', 'tool', 'capability'], '🔧', '🔥'),
         (['share', 'distribute', 'route'], '📡', '👑'),
         (['review', 'approve', 'queue'], '📋', '💅'),
         (['goal', 'metric', 'performance'], '🎯', '💋')]
    ),
}


def get_context_emoji(agent_name: str, text: str):
    """Return (thinking_emoji, done_emoji) based on agent personality and message context."""
    default_thinking, default_done = '💭', '✨'
    agent_key = agent_name.lower()
    if agent_key not in _AGENT_EMOJIS:
        return default_thinking, default_done

    sig_think, sig_done, rules = _AGENT_EMOJIS[agent_key]
    text_lower = text.lower()
    for keywords, t_emoji, d_emoji in rules:
        if any(kw in text_lower for kw in keywords):
            return t_emoji, d_emoji
    return sig_think, sig_done


class DiscordAdapter(BasePlatformAdapter):
    """
    Discord bot adapter.
    
    Handles:
    - Receiving messages from servers and DMs
    - Sending responses with Discord markdown
    - Thread support
    - Native slash commands (/ask, /reset, /status, /stop)
    - Button-based exec approvals
    - Auto-threading for long conversations
    - Reaction-based feedback
    - Turn-based inter-agent communication protocol
    """
    
    # Discord message limits
    MAX_MESSAGE_LENGTH = 2000
    
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self._client: Optional[commands.Bot] = None
        self._ready_event = asyncio.Event()
        self._allowed_user_ids: set = set()  # For button approval authorization
        self._bot_tracker = BotMessageTracker()  # Anti-doom-loop tracker
        self._group_chat = GroupChatSession()  # Multi-agent conversation tracker
        self._last_message_ts: Dict[str, float] = {}  # channel_id -> timestamp of our last message
        
        # Voice state
        self._voice_client: Optional[discord.VoiceClient] = None
        self._voice_sink: Optional[Any] = None
        # Voice is enabled if EITHER TTS key (for speaking) OR STT key (for listening) is set.
        # Previously this was gated only on ELEVENLABS_API_KEY, which blocked voice receive
        # when only VOICE_TOOLS_OPENAI_KEY was configured.
        self._voice_tts_enabled = os.getenv("ELEVENLABS_API_KEY") is not None
        self._voice_stt_enabled = os.getenv("VOICE_TOOLS_OPENAI_KEY") is not None
        self._voice_enabled = self._voice_tts_enabled or self._voice_stt_enabled
        self._voice_auto_join = os.getenv("DISCORD_VOICE_AUTO_JOIN", "false").lower() == "true"
        self._voice_channel_id = os.getenv("DISCORD_VOICE_CHANNEL_ID")
        self._voice_queue = asyncio.Queue()
        self._voice_worker_task: Optional[asyncio.Task] = None
        self._voice_keepalive_task: Optional[asyncio.Task] = None
        self._voice_volume = 1.0
        self._voice_muted = False

    
    async def connect(self) -> bool:
        """Connect to Discord and start receiving events."""
        if not DISCORD_AVAILABLE:
            logger.error("[%s] discord.py not installed. Run: pip install discord.py", self.name)
            return False
        
        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False
        
        try:
            # Set up intents -- members intent needed for username-to-ID resolution
            intents = Intents.default()
            intents.message_content = True
            intents.dm_messages = True
            intents.guild_messages = True
            intents.members = True
            intents.voice_states = True  # Required for voice receiving
            
            # Create bot
            self._client = commands.Bot(
                command_prefix="!",  # Not really used, we handle raw messages
                intents=intents,
            )
            
            # Parse allowed user entries (may contain usernames or IDs)
            allowed_env = os.getenv("DISCORD_ALLOWED_USERS", "")
            if allowed_env:
                self._allowed_user_ids = {
                    uid.strip() for uid in allowed_env.split(",") if uid.strip()
                }
            
            adapter_self = self  # capture for closure
            
            # Register event handlers
            @self._client.event
            async def on_ready():
                logger.info("[%s] Connected as %s", adapter_self.name, adapter_self._client.user)
                
                # Resolve any usernames in the allowed list to numeric IDs
                await adapter_self._resolve_allowed_usernames()
                
                # Sync slash commands with Discord
                try:
                    # If DISCORD_GUILD_ID is set, sync specifically to that guild for instant updates
                    guild_id = os.getenv("DISCORD_GUILD_ID")
                    if guild_id:
                        guild = discord.Object(id=int(guild_id))
                        adapter_self._client.tree.copy_global_to(guild=guild)
                        await adapter_self._client.tree.sync(guild=guild)
                        logger.info("[%s] Synced slash command(s) to guild %s", adapter_self.name, guild_id)
                    
                    # Also perform global sync (can take up to an hour)
                    synced_global = await adapter_self._client.tree.sync()
                    logger.info("[%s] Synced %d global slash command(s)", adapter_self.name, len(synced_global))
                except Exception as e:  # pragma: no cover - defensive logging
                    logger.warning("[%s] Slash command sync failed: %s", adapter_self.name, e, exc_info=True)
                adapter_self._ready_event.set()

                # ── Voice Auto-Join ──
                if adapter_self._voice_enabled and adapter_self._voice_auto_join and adapter_self._voice_channel_id:
                    try:
                        if not _has_ffmpeg():
                            logger.warning("[%s] FFmpeg not found. Voice auto-join skipped.", adapter_self.name)
                        else:
                            channel = adapter_self._client.get_channel(int(adapter_self._voice_channel_id))
                            if not channel:
                                channel = await adapter_self._client.fetch_channel(int(adapter_self._voice_channel_id))
                            if channel:
                                if VOICE_RECV_AVAILABLE:
                                    from gateway.platforms.voice_sink import VoiceSink
                                    adapter_self._voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
                                    adapter_self._voice_sink = VoiceSink(adapter_self, str(channel.id))
                                    adapter_self._voice_client.listen(adapter_self._voice_sink)
                                    adapter_self._voice_sink.start()
                                    # Start keepalive to prevent Discord from closing the receive socket
                                    adapter_self._voice_keepalive_task = asyncio.create_task(adapter_self._voice_keepalive())
                                    logger.info("[%s] Auto-joined voice channel %s with real-time receiving enabled", adapter_self.name, channel.name)
                                    # Send visible confirmation to home channel
                                    home_ch_id = os.getenv('DISCORD_HOME_CHANNEL', '')
                                    if home_ch_id:
                                        try:
                                            home_ch = adapter_self._client.get_channel(int(home_ch_id))
                                            if home_ch:
                                                status_parts = []
                                                if adapter_self._voice_stt_enabled:
                                                    status_parts.append("🎧 Listening (Whisper STT)")
                                                else:
                                                    status_parts.append("⚠️ Cannot listen (VOICE_TOOLS_OPENAI_KEY not set)")
                                                if adapter_self._voice_tts_enabled:
                                                    status_parts.append("🔊 Speaking (ElevenLabs TTS)")
                                                else:
                                                    status_parts.append("⚠️ Cannot speak (ELEVENLABS_API_KEY not set)")
                                                status_str = " | ".join(status_parts)
                                                await home_ch.send(f"🎙️ **Voice active** — joined **{channel.name}**\n{status_str}")
                                        except Exception:
                                            pass
                                else:
                                    adapter_self._voice_client = await channel.connect()
                                    logger.info("[%s] Auto-joined voice channel %s (receive NOT available — install discord-ext-voice-recv)", adapter_self.name, channel.name)
                                    # Warn user that receiving is not available
                                    home_ch_id = os.getenv('DISCORD_HOME_CHANNEL', '')
                                    if home_ch_id:
                                        try:
                                            home_ch = adapter_self._client.get_channel(int(home_ch_id))
                                            if home_ch:
                                                await home_ch.send("⚠️ **Voice joined but receiving DISABLED** — `discord-ext-voice-recv` is not installed. I can speak but cannot hear you.")
                                        except Exception:
                                            pass
                    except Exception as ve:
                        logger.error("[%s] Voice auto-join failed: %s", adapter_self.name, ve, exc_info=True)
                        home_ch_id = os.getenv('DISCORD_HOME_CHANNEL', '')
                        if home_ch_id:
                            try:
                                home_ch = adapter_self._client.get_channel(int(home_ch_id))
                                if home_ch:
                                    await home_ch.send(f"⚠️ **Voice auto-join failed**: {ve}")
                            except Exception:
                                pass
                
                # ── Post-deployment startup notification ──
                # Send a message to the agent's home channel so the owner
                # can see who's online without checking Railway.
                try:
                    agent_name = os.getenv('AGENT_NAME', 'unknown').capitalize()
                    home_channel_id = os.getenv('DISCORD_HOME_CHANNEL', '')
                    if home_channel_id:
                        channel = adapter_self._client.get_channel(int(home_channel_id))
                        if not channel:
                            channel = await adapter_self._client.fetch_channel(int(home_channel_id))
                        if channel:
                            import datetime
                            now = datetime.datetime.now(datetime.timezone.utc)
                            timestamp = now.strftime('%b %d, %I:%M %p UTC')
                            model_name = os.getenv('HERMES_MODEL', 'default')
                            # Pick a signature emoji for the agent
                            agent_key = agent_name.lower()
                            emoji_map = {
                                'harmony': '\U0001f451',   # crown
                                'jade': '\U0001f3b5',      # music note
                                'lexi': '\U0001f4da',      # books
                                'sasha': '\U0001f3af',     # target
                                'scarlett': '\U0001f49d',  # heart ribbon
                                'raven': '\U0001f50d',     # magnifying glass
                                'cora': '\U0001f3a8',      # palette
                                'sabrina': '\U0001f4f1',   # phone
                                'addison': '\U0001f4e2',   # loudspeaker
                                'samantha': '\U0001f4cb',  # clipboard
                                'tatiana': '\U0001f3e0',   # house
                                'valentina': '\u2699\ufe0f',  # gear
                                'bianca': '\U0001f4ca',    # chart
                            }
                            emoji = emoji_map.get(agent_key, '\u2705')
                            await channel.send(
                                f"{emoji} **{agent_name}** is online and ready.\n"
                                f"> Deployed: {timestamp}\n"
                                f"> Slash commands synced: {len(synced)}\n"
                                f"> Status: All systems operational"
                            )
                            logger.info("[%s] Startup notification sent to channel %s", adapter_self.name, home_channel_id)
                except Exception as e:
                    logger.warning("[%s] Failed to send startup notification: %s", adapter_self.name, e)
            
            @self._client.event
            async def on_message(message: DiscordMessage):
                # Always ignore our own messages
                if message.author == self._client.user:
                    return

                # FALLBACK: !sync command to force command tree synchronization
                if message.content.strip().lower() == "!sync":
                    # For debugging, we allow any message in the home channel to trigger sync
                    # or if the user is the guild owner/allowed user.
                    home_channel_id = os.getenv("DISCORD_HOME_CHANNEL", "")
                    is_home_channel = (str(message.channel.id) == home_channel_id)
                    
                    if is_home_channel or str(message.author.id) in adapter_self._allowed_user_ids or (message.guild and message.author.id == message.guild.owner_id):
                        try:
                            await message.channel.send("🔄 **Force-syncing slash commands to this server...**")
                            # Sync to current guild (instant)
                            if message.guild:
                                guild = discord.Object(id=message.guild.id)
                                adapter_self._client.tree.copy_global_to(guild=guild)
                                synced = await adapter_self._client.tree.sync(guild=guild)
                                # Sync globally (takes time)
                                await adapter_self._client.tree.sync()
                                await message.channel.send(f"✅ **Success!** Registered {len(synced)} commands (including `/voicekick` and `/voicestatus`) to this server. They should appear in your `/` menu immediately.")
                            else:
                                await message.channel.send("⚠️ This command must be used in a server channel to sync guild commands.")
                            return
                        except Exception as e:
                            await message.channel.send(f"❌ **Sync failed**: {e}")
                            logger.error("[%s] Manual sync failed: %s", adapter_self.name, e, exc_info=True)
                            return
                
                is_bot_message = getattr(message.author, "bot", False)
                is_owner = str(message.author.id) == OWNER_USER_ID
                agent_name = os.getenv('AGENT_NAME', '').lower()
                my_bot_id = str(self._client.user.id) if self._client.user else ''
                channel_id = str(message.channel.id)
                is_harmony = (agent_name == 'harmony')
                
                # ── "end chat" kill switch — owner can stop any group conversation ──
                if is_owner and message.content.strip().lower() in ('end chat', 'endchat', 'stop chat'):
                    if adapter_self._group_chat.is_active(channel_id):
                        adapter_self._group_chat.deactivate(channel_id)
                        try:
                            await message.add_reaction('\U0001f6d1')  # stop sign
                        except Exception:
                            pass
                        return
                
                # ── Owner @mentions 2+ bots → activate group chat mode ──
                if is_owner and not is_bot_message:
                    mentioned_bots = [
                        m for m in message.mentions
                        if getattr(m, 'bot', False) and m != self._client.user
                    ]
                    # Count ourselves if we're mentioned too
                    i_am_mentioned = self._client.user in message.mentions
                    total_bots_mentioned = len(mentioned_bots) + (1 if i_am_mentioned else 0)
                    
                    if total_bots_mentioned >= 2:
                        # Activate group chat with all mentioned bot IDs
                        participant_ids = {str(m.id) for m in mentioned_bots}
                        if i_am_mentioned:
                            participant_ids.add(my_bot_id)
                        adapter_self._group_chat.activate(channel_id, participant_ids, str(message.id))
                        logger.info("[%s] Group chat activated by owner with %d bots", agent_name, total_bots_mentioned)
                
                # ── Bot-to-bot communication protocol ──
                if is_bot_message:
                    allow_bots = os.getenv("DISCORD_ALLOW_BOTS", "none").lower().strip()
                    sender_id = str(message.author.id)
                    msg_id = str(message.id)
                    
                    # ── GROUP CHAT MODE: agents can talk to each other ──
                    if adapter_self._group_chat.is_active(channel_id):
                        # In group chat, only process if I'm @mentioned
                        if not self._client.user or self._client.user not in message.mentions:
                            return
                        
                        # Anti-doom-loop: already responded?
                        if adapter_self._bot_tracker.has_responded(msg_id):
                            return
                        
                        # 4-second delay: wait, then scrape recent messages for context
                        await asyncio.sleep(GROUP_CHAT_RESPONSE_DELAY)
                        
                        # Scrape recent messages since our last message for full context
                        context_messages = []
                        try:
                            async for hist_msg in message.channel.history(limit=20, after=None):
                                if hist_msg.id == message.id:
                                    continue
                                # Only include messages from the last 5 minutes
                                age = (message.created_at - hist_msg.created_at).total_seconds()
                                if age > 300:
                                    break
                                context_messages.append(f"{hist_msg.author.display_name}: {hist_msg.content}")
                        except Exception as e:
                            logger.warning("[%s] Failed to scrape context: %s", agent_name, e)
                        
                        # Build enriched message with conversation context
                        if context_messages:
                            context_messages.reverse()  # chronological order
                            context_block = "\n".join(context_messages[-10:])  # last 10 messages
                            message.content = (
                                f"[GROUP CONVERSATION CONTEXT]\n{context_block}\n\n"
                                f"[LATEST MESSAGE FROM {message.author.display_name}]\n"
                                f"{strip_message_tags(message.content)}\n\n"
                                f"Respond naturally to continue the conversation. "
                                f"If you want to direct your response to a specific agent, @mention them. "
                                f"Keep responses focused and conversational."
                            )
                        else:
                            message.content = strip_message_tags(message.content)
                        
                        # Update activity and mark responded
                        adapter_self._group_chat.touch(channel_id)
                        adapter_self._bot_tracker.mark_responded(msg_id)
                        # Fall through to process the message
                    else:
                        # ── NORMAL BOT-TO-BOT PROTOCOL (not group chat) ──
                        
                        # If bot messages completely disabled, drop it
                        if allow_bots == "none":
                            return
                        
                        # ── HARMONY FIX: In harmony-communication, Harmony processes
                        #    all tagged bot messages even without being @mentioned ──
                        is_in_harmony_channel = (channel_id == HARMONY_CHANNEL_ID)
                        tag = parse_message_tag(message.content)
                        
                        if allow_bots == "mentions":
                            explicitly_mentioned = (self._client.user and self._client.user in message.mentions)
                            # Harmony auto-processes tagged messages in her channel
                            harmony_auto = (is_harmony and is_in_harmony_channel and tag in ('REQUEST', 'NOTIFY', 'END', 'REPORT'))
                            if not explicitly_mentioned and not harmony_auto:
                                return
                        
                        # ── Anti-doom-loop: have we already responded to this message? ──
                        if adapter_self._bot_tracker.has_responded(msg_id):
                            return
                        
                        # ── [NOTIFY] or [END] - React only, do NOT reply ──
                        if tag in ('NOTIFY', 'END'):
                            _, done_emoji = get_context_emoji(agent_name, message.content)
                            try:
                                await message.add_reaction(done_emoji)
                            except Exception:
                                try:
                                    await message.add_reaction('\u2705')
                                except Exception:
                                    pass
                            adapter_self._bot_tracker.mark_responded(msg_id)
                            # For Harmony: [END] messages are task completions - log for awareness
                            if is_harmony and tag == 'END':
                                message.content = f'[TASK COMPLETED - Agent reported completion] {strip_message_tags(message.content)}'
                                # Fall through to process so Harmony can update task board
                            else:
                                return
                        
                        # ── Channel-based routing for worker agents ──
                        if not is_harmony:
                            # Workers only accept [REQUEST] from Harmony (or in group chat, handled above)
                            if tag == 'REQUEST' and sender_id != HARMONY_BOT_ID:
                                try:
                                    await message.add_reaction('\u26d4')  # no_entry
                                except Exception:
                                    pass
                                adapter_self._bot_tracker.mark_responded(msg_id)
                                return
                        
                        # ── Check if this is a reply to one of MY messages ──
                        is_reply_to_me = False
                        if message.reference and message.reference.message_id:
                            try:
                                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                                if ref_msg.author == self._client.user:
                                    is_reply_to_me = True
                            except Exception:
                                pass
                        
                        # ── No tag from a bot ──
                        if tag is None:
                            if is_reply_to_me:
                                adapter_self._bot_tracker.mark_responded(msg_id)
                                message.content = f'[AGENT RESPONSE - DO NOT REPLY TO THIS AGENT] {message.content}'
                            else:
                                _, done_emoji = get_context_emoji(agent_name, message.content)
                                try:
                                    await message.add_reaction(done_emoji)
                                except Exception:
                                    try:
                                        await message.add_reaction('\ud83d\udc4d')
                                    except Exception:
                                        pass
                                adapter_self._bot_tracker.mark_responded(msg_id)
                                return
                        
                        # ── [REQUEST] or [REPORT] - Allow exactly one response ──
                        adapter_self._bot_tracker.mark_responded(msg_id)
                        
                        # Strip the tag before passing to LLM
                        message.content = strip_message_tags(message.content)
                
                # ── Human messages (and approved bot messages) ──
                # Context-aware emoji reactions
                thinking_emoji, done_emoji = get_context_emoji(agent_name, message.content)

                try:
                    await message.add_reaction(thinking_emoji)
                except Exception:
                    pass

                await self._handle_message(message)

                try:
                    await message.remove_reaction(thinking_emoji, self._client.user)
                    await message.add_reaction(done_emoji)
                except Exception:
                    pass
            
            # Register slash commands
            self._register_slash_commands()
            
            # Start the bot in background
            asyncio.create_task(self._client.start(self.config.token))
            
            # Wait for ready
            await asyncio.wait_for(self._ready_event.wait(), timeout=30)
            
            # Start voice worker
            self._voice_worker_task = asyncio.create_task(self._voice_worker())
            
            self._running = True
            return True
            
        except asyncio.TimeoutError:
            logger.error("[%s] Timeout waiting for connection to Discord", self.name, exc_info=True)
            return False
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to connect to Discord: %s", self.name, e, exc_info=True)
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from Discord."""
        if self._voice_worker_task:
            self._voice_worker_task.cancel()
            self._voice_worker_task = None

        if self._client:
            try:
                await self._client.close()
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning("[%s] Error during disconnect: %s", self.name, e, exc_info=True)
        
        self._running = False
        self._client = None
        self._ready_event.clear()
        logger.info("[%s] Disconnected", self.name)
    
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """Send a message to a Discord channel."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        
        try:
            # Get the channel
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
            
            # Format and split message if needed
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
            
            message_ids = []
            reference = None
            
            if reply_to:
                try:
                    ref_msg = await channel.fetch_message(int(reply_to))
                    reference = ref_msg
                except Exception as e:
                    logger.debug("Could not fetch reply-to message: %s", e)
            
            for i, chunk in enumerate(chunks):
                msg = await channel.send(
                    content=chunk,
                    reference=reference if i == 0 else None,
                )
                message_ids.append(str(msg.id))
            
            # ── VOICE STREAMING (TTS) ──
            # If we are in a voice channel AND TTS is configured, stream the sanitized content
            if self._voice_client and self._voice_client.is_connected() and self._voice_tts_enabled:
                if not _has_ffmpeg():
                    logger.warning("[%s] FFmpeg not found. Voice streaming skipped.", self.name)
                else:
                    try:
                        sanitized_text = sanitize_for_voice(content)
                        if sanitized_text:
                            # Use ElevenLabs to generate speech and stream it
                            from elevenlabs.client import ElevenLabs
                            api_key = os.getenv("ELEVENLABS_API_KEY")
                            if not api_key:
                                logger.error("[%s] ELEVENLABS_API_KEY not found.", self.name)
                            else:
                                client = ElevenLabs(api_key=api_key)
                                
                                # Generate audio stream with latency optimization
                                # We use a lower latency setting for live chat
                                optimize_latency = os.getenv("ELEVENLABS_LATENCY_LEVEL", "3")
                                
                                audio_stream = client.text_to_speech.convert(
                                    text=sanitized_text,
                                    voice_id=os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgmqS2iNRB47"),
                                    model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
                                    output_format="mp3_44100_128",
                                    optimize_streaming_latency=int(optimize_latency)
                                )
                                
                                # CHUNKED STREAMING: To reduce latency, we process the stream in chunks
                                # and put each chunk into the voice queue as it arrives.
                                # This allows the bot to start speaking before the full audio is generated.
                                chunk_size = 128 * 1024  # ~128KB chunks (~1-2 seconds of audio)
                                current_chunk = bytearray()
                                chunks_added = 0
                                
                                for chunk in audio_stream:
                                    current_chunk.extend(chunk)
                                    if len(current_chunk) >= chunk_size:
                                        await self._voice_queue.put(bytes(current_chunk))
                                        chunks_added += 1
                                        current_chunk = bytearray()
                                        # Log only the first chunk to show we started streaming
                                        if chunks_added == 1:
                                            logger.info("[%s] Started streaming first audio chunk to voice queue", self.name)
                                
                                # Don't forget the last partial chunk
                                if current_chunk:
                                    await self._voice_queue.put(bytes(current_chunk))
                                    chunks_added += 1
                                
                                logger.info("[%s] Added %d audio chunks to voice queue", self.name, chunks_added)
                    except Exception as ve:
                        logger.error("[%s] Voice streaming failed: %s", self.name, ve)

            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids}
            )
            
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send Discord message: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Edit a previously sent Discord message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            msg = await channel.fetch_message(int(message_id))
            formatted = self.format_message(content)
            if len(formatted) > self.MAX_MESSAGE_LENGTH:
                formatted = formatted[:self.MAX_MESSAGE_LENGTH - 3] + "..."
            await msg.edit(content=formatted)
            return SendResult(success=True, message_id=message_id)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to edit Discord message %s: %s", self.name, message_id, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send audio as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        
        try:
            import io
            
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
            
            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")
            
            filename = os.path.basename(audio_path)
            
            with open(audio_path, "rb") as f:
                file = discord.File(io.BytesIO(f.read()), filename=filename)
                msg = await channel.send(
                    content=caption if caption else None,
                    file=file,
                )
                return SendResult(success=True, message_id=str(msg.id))
        
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send voice: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a local image file natively as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            import io

            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            if not os.path.exists(image_path):
                return SendResult(success=False, error=f"Image file not found: {image_path}")

            filename = os.path.basename(image_path)
            
            with open(image_path, "rb") as f:
                file = discord.File(io.BytesIO(f.read()), filename=filename)
                msg = await channel.send(
                    content=caption if caption else None,
                    file=file,
                )
                return SendResult(success=True, message_id=str(msg.id))
        
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local image, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_image_file(chat_id, image_path, caption, reply_to)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send an image natively as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        
        try:
            import aiohttp
            
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
            
            # Download the image and send as a Discord file attachment
            # (Discord renders attachments inline, unlike plain URLs)
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to download image: HTTP {resp.status}")
                    
                    image_data = await resp.read()
                    
                    # Determine filename from URL or content type
                    content_type = resp.headers.get("content-type", "image/png")
                    ext = "png"
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = "jpg"
                    elif "gif" in content_type:
                        ext = "gif"
                    elif "webp" in content_type:
                        ext = "webp"
                    
                    import io
                    file = discord.File(io.BytesIO(image_data), filename=f"image.{ext}")
                    
                    msg = await channel.send(
                        content=caption if caption else None,
                        file=file,
                    )
                    return SendResult(success=True, message_id=str(msg.id))
        
        except ImportError:
            logger.warning(
                "[%s] aiohttp not installed, falling back to URL. Run: pip install aiohttp",
                self.name,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send image attachment, falling back to URL: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)
    
    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator."""
        if self._client:
            try:
                channel = self._client.get_channel(int(chat_id))
                if channel:
                    await channel.typing()
            except Exception:
                pass  # Ignore typing indicator failures
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Discord channel."""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}
        
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            
            if not channel:
                return {"name": str(chat_id), "type": "dm"}
            
            # Determine channel type
            if isinstance(channel, discord.DMChannel):
                chat_type = "dm"
                name = channel.recipient.name if channel.recipient else str(chat_id)
            elif isinstance(channel, discord.Thread):
                chat_type = "thread"
                name = channel.name
            elif isinstance(channel, discord.TextChannel):
                chat_type = "channel"
                name = f"#{channel.name}"
                if channel.guild:
                    name = f"{channel.guild.name} / {name}"
            else:
                chat_type = "channel"
                name = getattr(channel, "name", str(chat_id))
            
            return {
                "name": name,
                "type": chat_type,
                "guild_id": str(channel.guild.id) if hasattr(channel, "guild") and channel.guild else None,
                "guild_name": channel.guild.name if hasattr(channel, "guild") and channel.guild else None,
            }
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to get chat info for %s: %s", self.name, chat_id, e, exc_info=True)
            return {"name": str(chat_id), "type": "dm", "error": str(e)}
    
    async def _voice_worker(self):
        """Background worker to process the voice queue and play audio sequentially."""
        logger.info("[%s] Voice worker started", self.name)
        while True:
            try:
                audio_data = await self._voice_queue.get()
                if self._voice_client and self._voice_client.is_connected() and not self._voice_muted:
                    try:
                        # Wait for current audio to finish
                        while self._voice_client.is_playing():
                            await asyncio.sleep(0.1)
                        
                        source = discord.PCMVolumeTransformer(
                            discord.FFmpegPCMAudio(io.BytesIO(audio_data), pipe=True),
                            volume=self._voice_volume
                        )
                        # The 'play' method automatically sets the speaking state
                        self._voice_client.play(source)
                        logger.debug("[%s] Playing audio from queue", self.name)
                    except Exception as e:
                        logger.error("[%s] Error playing audio from queue: %s", self.name, e)
                self._voice_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] Voice worker error: %s", self.name, e)
                await asyncio.sleep(1)

    async def _voice_keepalive(self):
        """Send silent Opus packets to prevent Discord from closing the receive socket.
        
        Discord closes the listening socket if no audio is sent for a period.
        This background task sends a silent Opus frame every 10 seconds to
        keep the connection alive. We also toggle speaking state to ensure
        the socket stays fully active.
        """
        logger.info("[%s] Voice keepalive task started", self.name)
        # 20ms of silence (Opus frame)
        SILENT_OPUS_FRAME = b"\xf8\xff\xfe"
        while True:
            try:
                await asyncio.sleep(10)
                if self._voice_client and self._voice_client.is_connected():
                    if not self._voice_client.is_playing():
                        try:
                            # Briefly signal speaking to "wake up" the Discord socket
                            await self._voice_client.ws.speak(discord.SpeakingState.voice)
                            self._voice_client.send_audio_packet(SILENT_OPUS_FRAME, encode=False)
                            await asyncio.sleep(0.1)
                            await self._voice_client.ws.speak(discord.SpeakingState.none)
                        except Exception as e:
                            logger.debug("[%s] Keepalive packet failed: %s", self.name, e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] Voice keepalive error: %s", self.name, e)
                await asyncio.sleep(5)
        logger.info("[%s] Voice keepalive task stopped", self.name)

    async def _resolve_allowed_usernames(self) -> None:
        """
        Resolve non-numeric entries in DISCORD_ALLOWED_USERS to Discord user IDs.

        Users can specify usernames (e.g. "teknium") or display names instead of
        raw numeric IDs.  After resolution, the env var and internal set are updated
        so authorization checks work with IDs only.
        """
        if not self._allowed_user_ids or not self._client:
            return

        numeric_ids = set()
        to_resolve = set()

        for entry in self._allowed_user_ids:
            if entry.isdigit():
                numeric_ids.add(entry)
            else:
                to_resolve.add(entry.lower())

        if not to_resolve:
            return

        print(f"[{self.name}] Resolving {len(to_resolve)} username(s): {', '.join(to_resolve)}")
        resolved_count = 0

        for guild in self._client.guilds:
            # Fetch full member list (requires members intent)
            try:
                members = guild.members
                if len(members) < guild.member_count:
                    members = [m async for m in guild.fetch_members(limit=None)]
            except Exception as e:
                logger.warning("Failed to fetch members for guild %s: %s", guild.name, e)
                continue

            for member in members:
                name_lower = member.name.lower()
                display_lower = member.display_name.lower()
                global_lower = (member.global_name or "").lower()

                matched = name_lower in to_resolve or display_lower in to_resolve or global_lower in to_resolve
                if matched:
                    uid = str(member.id)
                    numeric_ids.add(uid)
                    resolved_count += 1
                    matched_name = name_lower if name_lower in to_resolve else (
                        display_lower if display_lower in to_resolve else global_lower
                    )
                    to_resolve.discard(matched_name)
                    print(f"[{self.name}] Resolved '{matched_name}' -> {uid} ({member.name}#{member.discriminator})")

            if not to_resolve:
                break

        if to_resolve:
            print(f"[{self.name}] Could not resolve usernames: {', '.join(to_resolve)}")

        # Update internal set and env var so gateway auth checks use IDs
        self._allowed_user_ids = numeric_ids
        os.environ["DISCORD_ALLOWED_USERS"] = ",".join(sorted(numeric_ids))
        if resolved_count:
            print(f"[{self.name}] Updated DISCORD_ALLOWED_USERS with {resolved_count} resolved ID(s)")

    def format_message(self, content: str) -> str:
        """
        Format message for Discord.
        
        Discord uses its own markdown variant.
        """
        # Discord markdown is fairly standard, no special escaping needed
        return content
    
    def _register_slash_commands(self) -> None:
        """Register Discord slash commands on the command tree."""
        if not self._client:
            return

        tree = self._client.tree

        @tree.command(name="ask", description="Ask Hermes a question")
        @discord.app_commands.describe(question="Your question for Hermes")
        async def slash_ask(interaction: discord.Interaction, question: str):
            await interaction.response.defer()
            event = self._build_slash_event(interaction, question)
            await self.handle_message(event)
            try:
                await interaction.followup.send("Processing complete~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="new", description="Start a new conversation")
        async def slash_new(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/reset")
            await self.handle_message(event)
            try:
                await interaction.followup.send("New conversation started~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="reset", description="Reset your Hermes session")
        async def slash_reset(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/reset")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Session reset~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="model", description="Show or change the model")
        @discord.app_commands.describe(name="Model name (e.g. anthropic/claude-sonnet-4). Leave empty to see current.")
        async def slash_model(interaction: discord.Interaction, name: str = ""):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/model {name}".strip())
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="personality", description="Set a personality")
        @discord.app_commands.describe(name="Personality name. Leave empty to list available.")
        async def slash_personality(interaction: discord.Interaction, name: str = ""):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/personality {name}".strip())
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="nsfw", description="Toggle NSFW/unfiltered mode on or off")
        @discord.app_commands.describe(toggle="on or off. Leave empty to check current state.")
        async def slash_nsfw(interaction: discord.Interaction, toggle: str = ""):
            await interaction.response.defer(ephemeral=True)
            toggle = toggle.strip().lower()
            if toggle in ("on", "yes", "true", "enable", "1"):
                event = self._build_slash_event(interaction, "/personality unfiltered")
                await self.handle_message(event)
                try:
                    await interaction.followup.send("\U0001f525 NSFW mode **enabled** — unfiltered personality active.\n_Use `/nsfw off` to return to normal._", ephemeral=True)
                except Exception as e:
                    logger.debug("Discord followup failed: %s", e)
            elif toggle in ("off", "no", "false", "disable", "0"):
                event = self._build_slash_event(interaction, "/personality normal")
                await self.handle_message(event)
                try:
                    await interaction.followup.send("\u2705 NSFW mode **disabled** — normal personality restored.\n_Use `/nsfw on` to re-enable._", ephemeral=True)
                except Exception as e:
                    logger.debug("Discord followup failed: %s", e)
            else:
                try:
                    await interaction.followup.send(
                        "**NSFW Toggle**\n\n"
                        "`/nsfw on` — Enable unfiltered mode (no content restrictions)\n"
                        "`/nsfw off` — Return to normal professional mode\n\n"
                        "_This sets the agent's personality. Use `/personality` to see all options._",
                        ephemeral=True
                    )
                except Exception as e:
                    logger.debug("Discord followup failed: %s", e)

        @tree.command(name="retry", description="Retry your last message")
        async def slash_retry(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/retry")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Retrying~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="undo", description="Remove the last exchange")
        async def slash_undo(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/undo")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="status", description="Show Hermes session status")
        async def slash_status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/status")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Status sent~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="sethome", description="Set this chat as the home channel")
        async def slash_sethome(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/sethome")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="stop", description="Stop the running Hermes agent")
        async def slash_stop(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/stop")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Stop requested~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="compress", description="Compress conversation context")
        async def slash_compress(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/compress")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="title", description="Set or show the session title")
        @discord.app_commands.describe(name="Session title. Leave empty to show current.")
        async def slash_title(interaction: discord.Interaction, name: str = ""):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/title {name}".strip())
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="resume", description="Resume a previously-named session")
        @discord.app_commands.describe(name="Session name to resume. Leave empty to list sessions.")
        async def slash_resume(interaction: discord.Interaction, name: str = ""):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/resume {name}".strip())
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="usage", description="Show token usage for this session")
        async def slash_usage(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/usage")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="provider", description="Show available providers")
        async def slash_provider(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/provider")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="join", description="Join a voice channel")
        @discord.app_commands.describe(
            channel="Name or ID of the voice channel to join (e.g., 'General' or voice channel ID). Leave empty to join your current channel."
        )
        async def slash_join(interaction: discord.Interaction, channel: str = None):
            if not self._voice_enabled:
                await interaction.response.send_message(
                    "Voice is not enabled. Set ELEVENLABS_API_KEY (for TTS) or VOICE_TOOLS_OPENAI_KEY (for STT) to enable voice features.",
                    ephemeral=True
                )
                return
            
            target_channel = None
            
            # If a channel name/ID was provided, try to find it
            if channel:
                # Try to find by ID first
                try:
                    channel_id = int(channel)
                    target_channel = self._client.get_channel(channel_id)
                    if not target_channel and interaction.guild:
                        # Try fetching from guild
                        try:
                            target_channel = await interaction.guild.fetch_channel(channel_id)
                        except Exception:
                            pass
                except ValueError:
                    # Not a valid ID, search by name in the guild
                    if interaction.guild:
                        channel_name_lower = channel.lower().strip()
                        for vc in interaction.guild.voice_channels:
                            if vc.name.lower() == channel_name_lower:
                                target_channel = vc
                                break
                        # If exact match failed, try partial match
                        if not target_channel:
                            for vc in interaction.guild.voice_channels:
                                if channel_name_lower in vc.name.lower():
                                    target_channel = vc
                                    break
                
                if not target_channel:
                    await interaction.response.send_message(
                        f"Could not find voice channel '{channel}'. Please provide the exact channel name or ID.",
                        ephemeral=True
                    )
                    return
            else:
                # No channel specified - use user's current voice channel or default
                if interaction.user.voice:
                    target_channel = interaction.user.voice.channel
                elif self._voice_channel_id:
                    target_channel = self._client.get_channel(int(self._voice_channel_id))
                
                if not target_channel:
                    await interaction.response.send_message(
                        "Please provide a voice channel name, join a voice channel first, or set DISCORD_VOICE_CHANNEL_ID~",
                        ephemeral=True
                    )
                    return
            
            await interaction.response.defer(ephemeral=True)
            try:
                # Stop existing sink if any
                if self._voice_sink:
                    self._voice_sink.stop()
                    self._voice_sink = None

                if self._voice_client and self._voice_client.is_connected():
                    await self._voice_client.move_to(target_channel)
                else:
                    if VOICE_RECV_AVAILABLE:
                        self._voice_client = await target_channel.connect(cls=voice_recv.VoiceRecvClient)
                    else:
                        self._voice_client = await target_channel.connect()
                
                # Start voice sink if available
                if VOICE_RECV_AVAILABLE:
                    from gateway.platforms.voice_sink import VoiceSink
                    self._voice_sink = VoiceSink(self, str(target_channel.id))
                    self._voice_client.listen(self._voice_sink)
                    self._voice_sink.start()
                    # Start keepalive to prevent Discord from closing the receive socket
                    if self._voice_keepalive_task:
                        self._voice_keepalive_task.cancel()
                    self._voice_keepalive_task = asyncio.create_task(self._voice_keepalive())
                    logger.info("[%s] Joined voice channel %s with real-time receiving", self.name, target_channel.name)
                    await interaction.followup.send(
                        f"🎙️ Joined **{target_channel.name}** with voice receiving **enabled**. I can hear you!",
                        ephemeral=True
                    )
                    # Also send a visible message to the home channel
                    home_ch_id = os.getenv('DISCORD_HOME_CHANNEL', '')
                    if home_ch_id:
                        try:
                            home_ch = self._client.get_channel(int(home_ch_id))
                            if home_ch:
                                await home_ch.send(f"🎙️ **Voice receiving active** — joined **{target_channel.name}**. Speak and I'll transcribe + respond.")
                        except Exception:
                            pass
                else:
                    await interaction.followup.send(
                        f"⚠️ Joined **{target_channel.name}** but voice receiving is **disabled** (discord-ext-voice-recv not installed). I can speak but cannot hear you.",
                        ephemeral=True
                    )
            except Exception as e:
                logger.error("[%s] Failed to join voice: %s", self.name, e)
                await interaction.followup.send(f"Failed to join voice: {str(e)}", ephemeral=True)

        @tree.command(name="leave", description="Leave the voice channel")
        async def slash_leave(interaction: discord.Interaction):
            if self._voice_keepalive_task:
                self._voice_keepalive_task.cancel()
                self._voice_keepalive_task = None
            if self._voice_sink:
                self._voice_sink.stop()
                self._voice_sink = None
                
            if self._voice_client and self._voice_client.is_connected():
                await self._voice_client.disconnect()
                self._voice_client = None
                await interaction.response.send_message("Left voice channel~", ephemeral=True)
            else:
                await interaction.response.send_message("Not in a voice channel~", ephemeral=True)

        @tree.command(name="volume", description="Set the voice volume (0-200)")
        @discord.app_commands.describe(level="Volume level (0-200, default 100)")
        async def slash_volume(interaction: discord.Interaction, level: int):
            if not self._voice_enabled:
                await interaction.response.send_message("Voice is not enabled~", ephemeral=True)
                return
            
            # Clamp volume between 0 and 2.0 (200%)
            self._voice_volume = max(0.0, min(2.0, level / 100.0))
            
            # If currently playing, update volume on the fly
            if self._voice_client and self._voice_client.source:
                if isinstance(self._voice_client.source, discord.PCMVolumeTransformer):
                    self._voice_client.source.volume = self._voice_volume
            
            await interaction.response.send_message(f"Voice volume set to **{int(self._voice_volume * 100)}%**~", ephemeral=True)

        @tree.command(name="mute", description="Mute the agent's voice output")
        async def slash_mute(interaction: discord.Interaction):
            self._voice_muted = True
            if self._voice_client and self._voice_client.is_playing():
                self._voice_client.stop()
            await interaction.response.send_message("Agent voice muted~", ephemeral=True)

        @tree.command(name="unmute", description="Unmute the agent's voice output")
        async def slash_unmute(interaction: discord.Interaction):
            self._voice_muted = False
            await interaction.response.send_message("Agent voice unmuted~", ephemeral=True)

        @tree.command(name="voicekick", description="Force a fresh voice connection to reset the listening socket")
        async def slash_voicekick(interaction: discord.Interaction):
            if not self._voice_client or not self._voice_client.is_connected():
                await interaction.response.send_message("I'm not in a voice channel!", ephemeral=True)
                return
            
            channel = self._voice_client.channel
            await interaction.response.send_message(f"🔄 Resetting voice connection to **{channel.name}**...", ephemeral=True)
            
            try:
                # Cleanup existing
                if self._voice_keepalive_task:
                    self._voice_keepalive_task.cancel()
                if self._voice_sink:
                    self._voice_sink.stop()
                await self._voice_client.disconnect()
                
                await asyncio.sleep(1)
                
                # Reconnect fresh
                if VOICE_RECV_AVAILABLE:
                    self._voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
                    from gateway.platforms.voice_sink import VoiceSink
                    self._voice_sink = VoiceSink(self, str(channel.id))
                    self._voice_client.listen(self._voice_sink)
                    self._voice_sink.start()
                    self._voice_keepalive_task = asyncio.create_task(self._voice_keepalive())
                    await interaction.followup.send("✅ Voice connection reset. Try speaking now!", ephemeral=True)
                else:
                    self._voice_client = await channel.connect()
                    await interaction.followup.send("✅ Reconnected (receiving still disabled).", ephemeral=True)
            except Exception as e:
                logger.error("[%s] Voice kick failed: %s", self.name, e)
                await interaction.followup.send(f"❌ Reset failed: {e}", ephemeral=True)

        @tree.command(name="voicestatus", description="Show voice pipeline diagnostic status")
        async def slash_voicestatus(interaction: discord.Interaction):
            lines = []
            lines.append("**Voice Pipeline Diagnostic Status**")
            lines.append(f"")
            lines.append(f"VOICE_RECV_AVAILABLE: `{VOICE_RECV_AVAILABLE}`")
            lines.append(f"Voice enabled (any voice key set): `{self._voice_enabled}`")
            lines.append(f"Voice TTS enabled (ElevenLabs): `{self._voice_tts_enabled}`")
            lines.append(f"Voice STT enabled (Whisper): `{self._voice_stt_enabled}`")
            lines.append(f"Voice auto-join: `{self._voice_auto_join}`")
            lines.append(f"Voice channel ID: `{self._voice_channel_id}`")
            lines.append(f"")

            if self._voice_client:
                lines.append(f"Voice client connected: `{self._voice_client.is_connected()}`")
                lines.append(f"Voice client is playing: `{self._voice_client.is_playing()}`")
                vc_channel = getattr(self._voice_client, 'channel', None)
                lines.append(f"Voice client channel: `{vc_channel}`")
                # Check if it's a VoiceRecvClient
                is_recv = hasattr(self._voice_client, 'listen')
                lines.append(f"Is VoiceRecvClient (has listen): `{is_recv}`")
                is_listening = False
                if hasattr(self._voice_client, 'is_listening'):
                    is_listening = self._voice_client.is_listening()
                lines.append(f"Is listening: `{is_listening}`")
            else:
                lines.append("Voice client: `None` (not connected)")

            lines.append(f"")
            if self._voice_sink:
                lines.append(f"Voice sink active: `True`")
                lines.append(f"Sink packet count: `{self._voice_sink._packet_count}`")
                lines.append(f"Sink first packet logged: `{self._voice_sink._first_packet_logged}`")
                lines.append(f"Sink running: `{self._voice_sink._running}`")
                lines.append(f"Sink write errors: `{self._voice_sink._write_errors}`")
                lines.append(f"Sink last error: `{self._voice_sink._last_error or 'none'}`")
                lines.append(f"Sink event loop: `{self._voice_sink._loop is not None}`")
                lines.append(f"Sink audio buffers: `{len(self._voice_sink.audio_data)}` users")
                for uid, buf in self._voice_sink.audio_data.items():
                    lines.append(f"  - User `{uid}`: `{len(buf)}` bytes buffered")
            else:
                lines.append("Voice sink: `None` (not active)")

            lines.append(f"")
            lines.append(f"Keepalive task running: `{self._voice_keepalive_task is not None and not self._voice_keepalive_task.done()}`")
            lines.append(f"Voice worker task running: `{self._voice_worker_task is not None and not self._voice_worker_task.done()}`")
            lines.append(f"Voice muted: `{self._voice_muted}`")
            lines.append(f"Voice volume: `{self._voice_volume}`")

            lines.append(f"")
            lines.append(f"VOICE_TOOLS_OPENAI_KEY set: `{bool(os.getenv('VOICE_TOOLS_OPENAI_KEY'))}`")
            lines.append(f"ELEVENLABS_API_KEY set: `{bool(os.getenv('ELEVENLABS_API_KEY'))}`")
            lines.append(f"DISCORD_HOME_CHANNEL: `{os.getenv('DISCORD_HOME_CHANNEL', 'not set')}`")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @tree.command(name="help", description="Show available commands")
        async def slash_help(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/help")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="insights", description="Show usage insights and analytics")
        @discord.app_commands.describe(days="Number of days to analyze (default: 7)")
        async def slash_insights(interaction: discord.Interaction, days: int = 7):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/insights {days}")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="reload-mcp", description="Reload MCP servers from config")
        async def slash_reload_mcp(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/reload-mcp")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Done~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

        @tree.command(name="update", description="Update Hermes Agent to the latest version")
        async def slash_update(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, "/update")
            await self.handle_message(event)
            try:
                await interaction.followup.send("Update initiated~", ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

    def _build_slash_event(self, interaction: discord.Interaction, text: str) -> MessageEvent:
        """Build a MessageEvent from a Discord slash command interaction."""
        is_dm = isinstance(interaction.channel, discord.DMChannel)
        chat_type = "dm" if is_dm else "group"
        chat_name = ""
        if not is_dm and hasattr(interaction.channel, "name"):
            chat_name = interaction.channel.name
            if hasattr(interaction.channel, "guild") and interaction.channel.guild:
                chat_name = f"{interaction.channel.guild.name} / #{chat_name}"
        
        # Get channel topic (if available)
        chat_topic = getattr(interaction.channel, "topic", None)

        source = self.build_source(
            chat_id=str(interaction.channel_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            chat_topic=chat_topic,
        )

        msg_type = MessageType.COMMAND if text.startswith("/") else MessageType.TEXT
        return MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=interaction,
        )

    async def send_exec_approval(
        self, chat_id: str, command: str, approval_id: str
    ) -> SendResult:
        """
        Send a button-based exec approval prompt for a dangerous command.

        Returns SendResult. The approval is resolved when a user clicks a button.
        """
        if not self._client or not DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")

        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))

            embed = discord.Embed(
                title="Command Approval Required",
                description=f"```\n{command[:500]}\n```",
                color=discord.Color.orange(),
            )
            embed.set_footer(text=f"Approval ID: {approval_id}")

            view = ExecApprovalView(
                approval_id=approval_id,
                allowed_user_ids=self._allowed_user_ids,
            )

            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    def _get_parent_channel_id(self, channel: Any) -> Optional[str]:
        """Return the parent channel ID for a Discord thread-like channel, if present."""
        parent = getattr(channel, "parent", None)
        if parent is not None and getattr(parent, "id", None) is not None:
            return str(parent.id)
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            return str(parent_id)
        return None

    def _is_forum_parent(self, channel: Any) -> bool:
        """Best-effort check for whether a Discord channel is a forum channel."""
        if channel is None:
            return False
        forum_cls = getattr(discord, "ForumChannel", None)
        if forum_cls and isinstance(channel, forum_cls):
            return True
        channel_type = getattr(channel, "type", None)
        if channel_type is not None:
            type_value = getattr(channel_type, "value", channel_type)
            if type_value == 15:
                return True
        return False

    def _format_thread_chat_name(self, thread: Any) -> str:
        """Build a readable chat name for thread-like Discord channels, including forum context when available."""
        thread_name = getattr(thread, "name", None) or str(getattr(thread, "id", "thread"))
        parent = getattr(thread, "parent", None)
        guild = getattr(thread, "guild", None) or getattr(parent, "guild", None)
        guild_name = getattr(guild, "name", None)
        parent_name = getattr(parent, "name", None)

        if self._is_forum_parent(parent) and guild_name and parent_name:
            return f"{guild_name} / {parent_name} / {thread_name}"
        if parent_name and guild_name:
            return f"{guild_name} / #{parent_name} / {thread_name}"
        if parent_name:
            return f"{parent_name} / {thread_name}"
        return thread_name

    async def _handle_message(self, message: DiscordMessage) -> None:
        """Handle incoming Discord messages."""
        # In server channels (not DMs), require the bot to be @mentioned
        # UNLESS the channel is in the free-response list.
        #
        # Config:
        #   DISCORD_FREE_RESPONSE_CHANNELS: Comma-separated channel IDs where the
        #       bot responds to every message without needing a mention.
        #   DISCORD_REQUIRE_MENTION: Set to "false" to disable mention requirement
        #       globally (all channels become free-response). Default: "true".
        #       Can also be set via discord.require_mention in config.yaml.

        thread_id = None
        parent_channel_id = None
        is_thread = isinstance(message.channel, discord.Thread)
        if is_thread:
            thread_id = str(message.channel.id)
            parent_channel_id = self._get_parent_channel_id(message.channel)

        if not isinstance(message.channel, discord.DMChannel):
            free_channels_raw = os.getenv("DISCORD_FREE_RESPONSE_CHANNELS", "")
            free_channels = {ch.strip() for ch in free_channels_raw.split(",") if ch.strip()}
            channel_ids = {str(message.channel.id)}
            if parent_channel_id:
                channel_ids.add(parent_channel_id)

            require_mention = os.getenv("DISCORD_REQUIRE_MENTION", "false").lower() not in ("false", "0", "no")
            
            # The agent's home channel is always free-response so the
            # owner can talk naturally without @mentioning.
            home_ch = os.getenv("DISCORD_HOME_CHANNEL", "")
            if home_ch:
                free_channels.add(home_ch.strip())
            
            is_free_channel = bool(channel_ids & free_channels)
            
            # ── VOICE MESSAGE BYPASS ──
            # Messages starting with [VOICE] (transcribed by VoiceSink) 
            # NEVER require a mention.
            is_voice_msg = message.content.startswith("[VOICE]")

            if require_mention and not is_free_channel and not is_voice_msg:
                if self._client.user not in message.mentions:
                    return

            if self._client.user and self._client.user in message.mentions:
                message.content = message.content.replace(f"<@{self._client.user.id}>", "").strip()
                message.content = message.content.replace(f"<@!{self._client.user.id}>", "").strip()

        # Determine message type
        msg_type = MessageType.TEXT
        if message.content.startswith("/"):
            msg_type = MessageType.COMMAND
        elif message.attachments:
            # Check attachment types
            for att in message.attachments:
                if att.content_type:
                    if att.content_type.startswith("image/"):
                        msg_type = MessageType.PHOTO
                    elif att.content_type.startswith("video/"):
                        msg_type = MessageType.VIDEO
                    elif att.content_type.startswith("audio/"):
                        msg_type = MessageType.AUDIO
                    else:
                        msg_type = MessageType.DOCUMENT
                    break
        
        # Determine chat type
        if isinstance(message.channel, discord.DMChannel):
            chat_type = "dm"
            chat_name = message.author.name
        elif is_thread:
            chat_type = "thread"
            chat_name = self._format_thread_chat_name(message.channel)
        else:
            chat_type = "group"
            chat_name = getattr(message.channel, "name", str(message.channel.id))
            if hasattr(message.channel, "guild") and message.channel.guild:
                chat_name = f"{message.channel.guild.name} / #{chat_name}"

        # Get channel topic (if available - TextChannels have topics, DMs/threads don't)
        chat_topic = getattr(message.channel, "topic", None)
        
        # Build source
        source = self.build_source(
            chat_id=str(message.channel.id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
        )
        
        # Build media URLs -- download image attachments to local cache so the
        # vision tool can access them reliably (Discord CDN URLs can expire).
        media_urls = []
        media_types = []
        for att in message.attachments:
            content_type = att.content_type or "unknown"
            if content_type.startswith("image/"):
                try:
                    # Determine extension from content type (image/png -> .png)
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                    cached_path = await cache_image_from_url(att.url, ext=ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user image: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache image attachment: {e}", flush=True)
                    # Fall back to the CDN URL if caching fails
                    media_urls.append(att.url)
                    media_types.append(content_type)
            elif content_type.startswith("audio/"):
                try:
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in (".ogg", ".mp3", ".wav", ".webm", ".m4a"):
                        ext = ".ogg"
                    cached_path = await cache_audio_from_url(att.url, ext=ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user audio: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache audio attachment: {e}", flush=True)
                    media_urls.append(att.url)
                    media_types.append(content_type)
            else:
                # Other attachments: keep the original URL
                media_urls.append(att.url)
                media_types.append(content_type)
        
        event = MessageEvent(
            text=message.content,
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.id),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=str(message.reference.message_id) if message.reference else None,
            timestamp=message.created_at,
        )
        
        await self.handle_message(event)


# ---------------------------------------------------------------------------
# Discord UI Components (outside the adapter class)
# ---------------------------------------------------------------------------

if DISCORD_AVAILABLE:

    class ExecApprovalView(discord.ui.View):
        """
        Interactive button view for exec approval of dangerous commands.

        Shows three buttons: Allow Once (green), Always Allow (blue), Deny (red).
        Only users in the allowed list can click. The view times out after 5 minutes.
        """

        def __init__(self, approval_id: str, allowed_user_ids: set):
            super().__init__(timeout=300)  # 5-minute timeout
            self.approval_id = approval_id
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            """Verify the user clicking is authorized."""
            if not self.allowed_user_ids:
                return True  # No allowlist = anyone can approve
            return str(interaction.user.id) in self.allowed_user_ids

        async def _resolve(
            self, interaction: discord.Interaction, action: str, color: discord.Color
        ):
            """Resolve the approval and update the message."""
            if self.resolved:
                await interaction.response.send_message(
                    "This approval has already been resolved~", ephemeral=True
                )
                return

            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to approve commands~", ephemeral=True
                )
                return

            self.resolved = True

            # Update the embed with the decision
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{action} by {interaction.user.display_name}")

            # Disable all buttons
            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

            # Store the approval decision
            try:
                from tools.approval import approve_permanent
                if action == "allow_once":
                    pass  # One-time approval handled by gateway
                elif action == "allow_always":
                    approve_permanent(self.approval_id)
            except ImportError:
                pass

        @discord.ui.button(label="Allow Once", style=discord.ButtonStyle.green)
        async def allow_once(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_once", discord.Color.green())

        @discord.ui.button(label="Always Allow", style=discord.ButtonStyle.blurple)
        async def allow_always(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_always", discord.Color.blue())

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
        async def deny(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "deny", discord.Color.red())

        async def on_timeout(self):
            """Handle view timeout -- disable buttons and mark as expired."""
            self.resolved = True
            for child in self.children:
                child.disabled = True
