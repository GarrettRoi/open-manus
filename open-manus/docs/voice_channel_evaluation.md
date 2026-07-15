# Voice Channel Capabilities Evaluation Report
## Open-Manus Repository Analysis

**Date:** March 2026
**Status:** Evaluation Complete
**Priority:** High

---

## Executive Summary

The open-manus repository has **good voice infrastructure** with TTS and STT capabilities, but there are **gaps in configuration documentation** and **some missing features** that need implementation.

### Overall Status
- ✅ **70% Complete** - Voice core is functional
- ⚠️ **20% Needs Polish** - Documentation and config
- ❌ **10% Missing** - Tests and platform-specific send_voice()

---

## ✅ Implemented Features

### 1. Text-to-Speech (TTS) - `tools/tts_tool.py`
| Feature | Status | Notes |
|---------|--------|-------|
| Edge TTS (free) | ✅ Working | Default, Microsoft Edge neural voices |
| ElevenLabs (premium) | ✅ Working | High-quality voices |
| OpenAI TTS | ✅ Working | gpt-4o-mini-tts model |
| Telegram Opus output | ✅ Working | For voice bubbles |
| MP3 output | ✅ Working | For other platforms |
| ffmpeg conversion | ⚠️ Needs verification | Edge TTS MP3 → Opus |

**Config: `~/.hermes/config.yaml`**
```yaml
tts:
  provider: "edge"  # or "elevenlabs", "openai"
  edge:
    voice: "en-US-AriaNeural"
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"
```

### 2. Speech-to-Text (STT) - `tools/transcription_tools.py`
| Feature | Status | Notes |
|---------|--------|-------|
| Whisper API | ✅ Working | OpenAI Whisper |
| whisper-1 model | ✅ Working | Cheapest option |
| gpt-4o-mini-transcribe | ✅ Supported | Better quality |
| gpt-4o-transcribe | ✅ Supported | Best quality |
| Auto-transcription | ✅ Working | Gateway auto-transcribes user voice |
| File size validation | ✅ Working | 25MB limit |

### 3. Platform Support

| Platform | Send Voice | Receive Voice | Notes |
|----------|------------|---------------|-------|
| Discord | ✅ Streaming | N/A | Real-time TTS to voice channels |
| Telegram | ✅ send_voice() | ✅ | Native voice bubbles |
| WhatsApp | ❌ Missing | ✅ | Can receive, can't send |
| Signal | ❌ Missing | ✅ | Can receive, can't send |
| Slack | ⚠️ Basic | N/A | Files as attachments only |

### 4. Discord Voice Features - `gateway/platforms/discord.py`
| Feature | Status | Notes |
|---------|--------|-------|
| Voice streaming | ✅ Working | Real-time TTS |
| Auto-join channel | ✅ Working | DISCORD_VOICE_AUTO_JOIN env var |
| Slash command /join | ✅ Working | Manual voice channel join |
| Slash command /leave | ✅ Working | Leave voice channel |
| Voice sanitization | ✅ Working | Removes code/URLs before speaking |
| FFmpeg dependency | ⚠️ Needs check | Required but not verified |

### 5. Voice Sanitizer Skill - `skills/voice_sanitizer/`
| Feature | Status | Notes |
|---------|--------|-------|
| Code block removal | ✅ Working | Removes ```code``` |
| URL simplification | ✅ Working | Replaces with "a link" |
| Protocol tag removal | ✅ Working | [REQUEST], [END], etc. |
| Inline code removal | ✅ Working | Removes `code` |

---

## ⚠️ Issues Found

### Issue #1: Missing FFmpeg Installation Check
**File:** `gateway/platforms/discord.py` (lines 749-777)
**Severity:** High

```python
# Line 771 Comment:
# Note: This requires FFmpeg to be installed in the sandbox
source = discord.FFmpegPCMAudio(io.BytesIO(audio_data), pipe=True)
```

**Problem:** Code assumes FFmpeg is installed but doesn't verify it.
**Impact:** Runtime crash when trying to stream voice without FFmpeg.
**Fix:** Add FFmpeg availability check

```python
# Add at startup:
def check_ffmpeg():
    return shutil.which("ffmpeg") is not None

# In voice streaming:
if not check_ffmpeg():
    logger.error("FFmpeg not found. Voice streaming disabled.")
    return
```

---

### Issue #2: Missing TTS Config in Example
**File:** `cli-config.yaml.example`
**Severity:** Medium

**Problem:** No `tts:` section in the config example.
**Impact:** Users don't know how to configure TTS.
**Fix:** Add tts config to cli-config.yaml.example

```yaml
tts:
  provider: "edge"  # Options: edge, elevenlabs, openai
  edge:
    voice: "en-US-AriaNeural"  # See: edge-tts --list-voices
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"  # Options: alloy, echo, fable, onyx, nova, shimmer
```

---

### Issue #3: Missing Voice Environment Variables
**File:** `.env.example`
**Severity:** Medium

**Problem:** Discord voice environment variables are not documented.
**Impact:** Users don't know how to enable Discord voice features.
**Fix:** Add to .env.example

```bash
# =============================================================================
# Discord Voice (Optional)
# =============================================================================
# For voice channel streaming with Discord:
# - ELEVENLABS_API_KEY: Required for voice streaming (or use Edge TTS)
# - DISCORD_VOICE_CHANNEL_ID: Channel ID to auto-join
# - DISCORD_VOICE_AUTO_JOIN: Set to "true" to auto-join on startup
# - ELEVENLABS_VOICE_ID: Voice ID for Discord streaming (optional, has default)

DISCORD_VOICE_CHANNEL_ID=
DISCORD_VOICE_AUTO_JOIN=false
ELEVENLABS_VOICE_ID=pNInz6obpgmqS2iNRB47
```

---

### Issue #4: WhatsApp Missing send_voice()
**File:** `gateway/platforms/whatsapp.py`
**Severity:** Medium

**Problem:** WhatsApp platform can receive voice messages but cannot send them.
**Impact:** Cannot respond with voice on WhatsApp.
**Fix:** Implement send_voice() method similar to Telegram's implementation.

---

### Issue #5: Signal Missing send_voice()
**File:** `gateway/platforms/signal.py`
**Severity:** Medium

**Problem:** Signal platform can receive voice messages but cannot send them.
**Impact:** Cannot respond with voice on Signal.
**Fix:** Implement send_voice() method.

---

### Issue #6: No Voice Tests
**File:** `tests/` directory
**Severity:** Medium

**Problem:** No tests for:
- TTS tool
- Transcription tool
- send_voice() methods
- Voice sanitization

**Impact:** No regression protection for voice features.
**Fix:** Add comprehensive test suite

---

## ❌ Missing Features (Nice to Have)

1. **Voice Queue Management** - Queue multiple TTS requests for Discord
2. **Volume Control** - /volume command for Discord voice
3. **Mute/Unmute** - /mute and /unmute commands
4. **Voice Activity Detection** - Know when users are talking
5. **Voice User Feedback** - Let users know when bot is speaking

---

## 🔧 Quick Fixes Checklist

### High Priority (Fix Now)
- [ ] Add FFmpeg check to Discord adapter
- [ ] Add tts: section to cli-config.yaml.example
- [ ] Add voice env vars to .env.example
- [ ] Verify FFmpeg in Dockerfile

### Medium Priority (Next Sprint)
- [ ] Implement send_voice() for WhatsApp
- [ ] Implement send_voice() for Signal
- [ ] Add voice integration tests
- [ ] Add voice configuration to README.md

### Low Priority (Future)
- [ ] Voice queue management
- [ ] Volume/mute controls
- [ ] Voice activity detection

---

## 📋 Setup Checklist for New Users

To enable voice functionality:

1. **Get API Keys:**
   - OpenAI API Key: https://platform.openai.com/api-keys (for STT)
   - ElevenLabs API Key: https://elevenlabs.io/ (optional, for premium TTS)

2. **Set Environment Variables:**
   ```bash
   VOICE_TOOLS_OPENAI_KEY=sk-...
   ELEVENLABS_API_KEY=...
   DISCORD_VOICE_CHANNEL_ID=1234567890
   DISCORD_VOICE_AUTO_JOIN=true
   ```

3. **Install Dependencies:**
   ```bash
   pip install discord.py[voice] PyNaCl elevenlabs edge-tts
   apt-get install ffmpeg  # Required for Discord voice streaming
   ```

4. **Configure TTS:**
   Edit `~/.hermes/config.yaml`:
   ```yaml
   tts:
     provider: edge  # or elevenlabs, openai
   ```

5. **Test:**
   - Send a voice message on Telegram → Should be transcribed
   - Use text_to_speech tool → Should generate audio
   - Join Discord voice channel → Should hear TTS responses

---

## Related Files

| File | Lines | Purpose |
|------|-------|---------|
| `tools/tts_tool.py` | ~450 | TTS generation |
| `tools/transcription_tools.py` | ~200 | STT transcription |
| `gateway/platforms/discord.py` | ~1350 | Discord voice streaming |
| `gateway/platforms/telegram.py` | ~400 | Telegram voice messages |
| `gateway/platforms/whatsapp.py` | ~300 | WhatsApp (receive only) |
| `gateway/platforms/signal.py` | ~200 | Signal (receive only) |
| `skills/voice_sanitizer/` | - | Text sanitization |
| `gateway/platforms/base.py` | ~550 | Base voice interface |

---

## Next Steps

1. Create GitHub issues for each item in Quick Fixes Checklist
2. Assign priorities based on High/Medium/Low categories
3. Tag issues with `voice`, `tts`, `stt` labels
4. Coordinate with the developer cluster for implementation

---

*Report generated by Valentina - Developer Cluster Lead*
