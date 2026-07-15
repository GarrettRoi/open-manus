# Voice Sanitizer

This skill cleans up text before it is sent to the Text-to-Speech (TTS) engine. It removes technical jargon, code blocks, and long URLs to ensure the agent sounds natural and conversational in voice chat.

## Usage

To sanitize text for speech, run the following command in the terminal:

```bash
python3 /app/skills/voice_sanitizer/sanitizer.py "Your long, technical text with `code` and https://links.com/long-url"
```

### Example Output
```text
Your long, technical text with and a link
```

## Best Practices
1. **Always Sanitize Before Speaking**: Never send raw LLM output directly to TTS if it contains code or URLs.
2. **Focus on Conversation**: The goal is to sound like a human, not a computer reading a log file.
3. **Use for Voice Channels**: This tool is specifically designed for the Discord voice streaming capability.
