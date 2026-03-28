
╔══════════════════════════════════════════════════════════════════════════════╗
║                    DISCORD VOICE CONVERSATION ANALYSIS                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

USE CASE: Add agent to voice channel → Have a conversation (voice in, voice out)

┌─────────────────────────────────────────────────────────────────────────────┐
│                           CURRENT STATE                                     │
└─────────────────────────────────────────────────────────────────────────────┘

✅ AGENT CAN HEAR YOU (TEXT → LLM)
   • Discord text messages → Agent receives and understands
   • This works perfectly

❌ AGENT CANNOT HEAR YOUR VOICE (VOICE → LLM) ← BIG GAP
   • Discord voice channel audio is NOT captured
   • No implementation of discord.py's audio receiving (Sinks)
   • User voice messages are never transcribed to text for LLM
   
✅ AGENT CAN SPEAK TO YOU VIA VOICE (LLM → VOICE)
   • Agent text response → ElevenLabs TTS
   • Audio streamed to Discord voice channel
   • This works if FFmpeg is installed (it is in Dockerfile)

┌─────────────────────────────────────────────────────────────────────────────┐
│                      WHAT'S MISSING: VOICE RECEIVE                          │
└─────────────────────────────────────────────────────────────────────────────┘

Discord.py provides a "Sink" system to receive voice audio:

  Required Implementation:
  ─────────────────────────
  1. discord.sinks WaveSink or PCM audio sink to capture user audio
  2. VoiceClient.start_recording(sink, callback) when user starts speaking
  3. Store captured audio to file
  4. Send audio to Whisper STT (transcription_tools.py)
  5. Pass transcribed text to LLM as user message
  6. Stop recording after user finishes speaking

  Files requiring NEW code:
  ─────────────────────────
  • gateway/platforms/discord.py - Add audio reception logic

┌─────────────────────────────────────────────────────────────────────────────┐
│                      WHAT WORKS: VOICE SEND                                 │
└─────────────────────────────────────────────────────────────────────────────┘

  Current Implementation:
  ─────────────────────────
  1. LLM generates text response
  2. gateway/run.py detects output has audio to send
  3. text_to_speech tool generates audio via ElevenLabs
  4. If agent is in Discord voice channel:
     • Sanitize text (remove code/URLs)
     • Stream via ElevenLabs API
     • Play via discord.FFmpegPCMAudio

  Files with WORKING code:
  ─────────────────────────
  • tools/tts_tool.py - TTS generation ✅
  • gateway/platforms/discord.py lines 749-777 - Voice streaming ✅
  • skills/voice_sanitizer/sanitizer.py - Text cleanup ✅

┌─────────────────────────────────────────────────────────────────────────────┐
│                      DEPENDENCY CHECK                                       │
└─────────────────────────────────────────────────────────────────────────────┘

✅ FFmpeg - Installed in Dockerfile (line 20)
✅ discord.py[voice] - In pyproject.toml (line 44)
✅ PyNaCl - Installed (required for discord.py voice) (line 44)
✅ ElevenLabs Python client - In requirements.txt

┌─────────────────────────────────────────────────────────────────────────────┐
│                      TO ACHIEVE YOUR GOAL                                   │
└─────────────────────────────────────────────────────────────────────────────┘

IMPLEMENTATION NEEDED:

1. Add Discord voice RECEIVE to gateway/platforms/discord.py:

   import discord.sinks
   
   class VoiceSink(discord.sinks.WaveSink):
       def __init__(self):
           super().__init__()
           self.audio_data = {}
   
   # When user starts speaking:
   voice_client.start_recording(
       sink,
       process_finished_audio,  # callback when done
       sync_start=True
   )
   
   # Process audio after user stops speaking:
   async def process_finished_audio(sink, channel, *args):
       # Save audio to file
       # Send to Whisper STT
       # Forward transcribed text to LLM

2. Detect when users speak and start/stop recording

3. Integrate with existing transcription_tools.py for STT

┌─────────────────────────────────────────────────────────────────────────────┐
│                      TASK BREAKDOWN                                         │
└─────────────────────────────────────────────────────────────────────────────┘

HIGH PRIORITY (To achieve voice conversation):
────────────────────────────────────────────────
1. Implement Discord voice audio reception (Sink system)
2. Integration: Voice audio → Whisper STT → LLM
3. Test end-to-end: User speaks → Agent hears → Agent responds via voice

MEDIUM PRIORITY (Polish):
─────────────────────────
4. Add FFmpeg check with graceful error handling
5. Add voice configuration docs
6. Handle multiple users speaking simultaneously

EXPECTED BEHAVIOR AFTER IMPLEMENTATION:
────────────────────────────────────────
1. User joins voice channel with agent
2. User speaks: "Hey Valentina, what's the weather?"
3. Agent uses WaveSink to capture audio
4. Audio saved → Whisper transcribes: "Hey Valentina, what's the weather?"
5. Transcribed text sent to LLM
6. LLM responds: "The weather is sunny and 72°F"
7. Response sanitized and sent to ElevenLabs
8. ElevenLabs generates speech audio
9. Audio streamed to Discord voice channel
10. Agent speaks: "The weather is sunny and 72°F"

