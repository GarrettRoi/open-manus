# Voice Channel - Action Items

## Status: Evaluation Complete - Ready for Implementation

### Immediate Actions Needed

#### 1. Add FFmpeg Check to Discord (HIGH PRIORITY)
```python
# gateway/platforms/discord.py - after imports
import shutil

def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

# In voice streaming section:
if not _has_ffmpeg():
    logger.warning("[%s] FFmpeg not found. Voice streaming disabled.", self.name)
    return
```

#### 2. Update cli-config.yaml.example (HIGH PRIORITY)
Add at the end of the file:
```yaml
# -----------------------------------------------------------------------------
# Text-to-Speech Configuration
# https://elevenlabs.io/ (optional, premium voices)
# Edge TTS is free and works out of the box
# -----------------------------------------------------------------------------
tts:
  provider: "edge"  # Options: edge, elevenlabs, openai
  edge:
    voice: "en-US-AriaNeural"  # Run: edge-tts --list-voices
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"  # alloy, echo, fable, onyx, nova, shimmer
```

#### 3. Update .env.example (HIGH PRIORITY)
Add Discord voice section:
```bash
# =============================================================================
# Discord Voice (Optional)
# =============================================================================
DISCORD_VOICE_CHANNEL_ID=
DISCORD_VOICE_AUTO_JOIN=false
ELEVENLABS_VOICE_ID=pNInz6obpgmqS2iNRB47
```

### Medium Priority Tasks

4. Update Dockerfile to ensure FFmpeg is installed
5. Implement WhatsApp send_voice()
6. Implement Signal send_voice()
7. Add voice integration tests

### Full Evaluation Report
See: `docs/voice_channel_evaluation.md`

---
Generated: March 2026
