import os
import logging
import requests

logger = logging.getLogger(__name__)

def transcribe_audio(audio_path: str) -> str:
    """Transcribe audio file using OpenAI Whisper API via OpenRouter or direct OpenAI."""
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("No API key found for transcription")
        return ""

    # Check if using OpenRouter or OpenAI
    is_openrouter = "openrouter.ai" in os.getenv("OPENROUTER_BASE_URL", "") or not os.getenv("OPENAI_API_KEY")
    
    url = "https://openrouter.ai/api/v1/audio/transcriptions" if is_openrouter else "https://api.openai.com/v1/audio/transcriptions"
    
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    
    files = {
        "file": (os.path.basename(audio_path), open(audio_path, "rb"), "audio/wav"),
        "model": (None, "whisper-1")
    }
    
    try:
        response = requests.post(url, headers=headers, files=files, timeout=30)
        response.raise_for_status()
        result = response.json()
        return result.get("text", "").strip()
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""
    finally:
        # Close the file handle
        files["file"][1].close()
