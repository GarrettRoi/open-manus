"""ElevenLabs voice picker — list top adult female voices and swap the
active TTS voice at runtime.

Used by the ``/voice voices`` and ``/voice set`` subcommands. The picker
pulls from the ElevenLabs shared voice library (filtered to adult female,
English, ranked by community usage), caches the result for an hour, and
lets the user swap the active voice by list number, name, or raw voice ID.

Swapping updates:
  * ``tts.elevenlabs.voice_id`` in ``~/.hermes/config.yaml`` (used by
    tools/tts_tool.py — the live TTS path), and
  * the ``ELEVENLABS_VOICE_ID`` env var in this process (legacy adapter path).

Note: the swap persists until the next redeploy restores the shipped
config.yaml (or a dashboard deploy-file edit overwrites it).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:  # non-POSIX fallback (dev only)
    fcntl = None

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elevenlabs.io/v1"
_CACHE_PATH = Path(os.getenv("TMPDIR", "/tmp")) / "hermes_voice_picker_cache.json"
_CACHE_TTL_SECONDS = 3600
TOP_VOICES_LIMIT = 15

_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{10,40}$")


def _api_key() -> Optional[str]:
    return os.getenv("ELEVENLABS_API_KEY") or None


def _config_path() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"


def _read_cache() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_refresh_lock = threading.Lock()


def get_cached_voices_nonblocking() -> List[Dict[str, Any]]:
    """Deadline-safe read for latency-bound callers (Discord autocomplete's
    3s SLA): return whatever is cached — even stale — immediately, and kick
    off a background refresh if the cache is missing or expired. Never does
    network I/O on the calling thread."""
    cached = _read_cache()
    fresh = cached and time.time() - cached.get("fetched_at", 0) < _CACHE_TTL_SECONDS
    if not fresh and _api_key() and _refresh_lock.acquire(blocking=False):
        def _refresh():
            try:
                fetch_top_female_voices(force_refresh=True)
            except Exception as exc:
                logger.warning("Background voice-list refresh failed: %s", exc)
            finally:
                _refresh_lock.release()

        threading.Thread(target=_refresh, name="voice-list-refresh", daemon=True).start()
    return (cached or {}).get("voices", [])


def fetch_top_female_voices(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Return the top adult female shared voices, ranked by community usage.

    Each entry: {voice_id, public_owner_id, name, accent, age, description,
    use_case, preview_url, cloned_by_count}. Cached for an hour.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached and time.time() - cached.get("fetched_at", 0) < _CACHE_TTL_SECONDS:
            return cached.get("voices", [])

    key = _api_key()
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    import requests

    voices: List[Dict[str, Any]] = []
    # Adult = young + middle_aged (the shared library's non-child adult ages).
    for age in ("young", "middle_aged"):
        resp = requests.get(
            f"{_API_BASE}/shared-voices",
            headers={"xi-api-key": key},
            params={
                "gender": "female",
                "age": age,
                "language": "en",
                "category": "professional",
                "sort": "cloned_by_count",
                "sort_direction": "desc",
                "page_size": TOP_VOICES_LIMIT,
            },
            timeout=20,
        )
        resp.raise_for_status()
        for v in resp.json().get("voices", []):
            voices.append({
                "voice_id": v.get("voice_id"),
                "public_owner_id": v.get("public_owner_id"),
                "name": v.get("name"),
                "accent": v.get("accent"),
                "age": v.get("age"),
                "description": (v.get("description") or "")[:120],
                "use_case": v.get("use_case"),
                "preview_url": v.get("preview_url"),
                "cloned_by_count": v.get("cloned_by_count", 0),
            })

    # Merge the two age buckets, dedupe, keep the overall top N by usage.
    seen = set()
    merged = []
    for v in sorted(voices, key=lambda x: -(x.get("cloned_by_count") or 0)):
        if v["voice_id"] and v["voice_id"] not in seen:
            seen.add(v["voice_id"])
            merged.append(v)
    merged = merged[:TOP_VOICES_LIMIT]

    try:
        fd, tmp = tempfile.mkstemp(dir=str(_CACHE_PATH.parent), prefix=".voice_cache_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"fetched_at": time.time(), "voices": merged}))
        os.replace(tmp, _CACHE_PATH)
    except OSError:
        pass
    return merged


def _ensure_in_library(voice: Dict[str, Any]) -> None:
    """Shared voices must be added to the account's library before TTS can
    use them. Idempotent: a 'voice already added' error is treated as OK."""
    owner = voice.get("public_owner_id")
    vid = voice.get("voice_id")
    if not owner or not vid:
        return
    import requests

    resp = requests.post(
        f"{_API_BASE}/voices/add/{owner}/{vid}",
        headers={"xi-api-key": _api_key()},
        json={"new_name": voice.get("name") or vid},
        timeout=20,
    )
    if resp.status_code >= 400 and "already" not in resp.text.lower():
        resp.raise_for_status()


def get_current_voice_id() -> Optional[str]:
    try:
        import yaml

        cfg = yaml.safe_load(_config_path().read_text(encoding="utf-8")) or {}
        tts = cfg.get("tts") or {}
        el = tts.get("elevenlabs") or (tts.get("providers") or {}).get("elevenlabs") or {}
        return el.get("voice_id") or os.getenv("ELEVENLABS_VOICE_ID")
    except (OSError, ValueError):
        return os.getenv("ELEVENLABS_VOICE_ID")


def resolve_voice(selector: str) -> Dict[str, Any]:
    """Resolve a user selector (list number, name, or raw voice ID) into a
    voice entry. Raw IDs not in the top list return a minimal entry."""
    selector = (selector or "").strip()
    if not selector:
        raise ValueError("No voice given")
    try:
        voices = fetch_top_female_voices()
    except Exception:
        voices = []  # picker list unavailable; raw IDs still work

    if selector.isdigit():
        idx = int(selector)
        if not voices:
            raise ValueError("Voice list unavailable — use a raw voice ID instead")
        if not 1 <= idx <= len(voices):
            raise ValueError(f"Pick a number between 1 and {len(voices)}")
        return voices[idx - 1]

    for v in voices:
        if selector.lower() == (v.get("name") or "").lower() or selector == v["voice_id"]:
            return v

    if _VOICE_ID_RE.match(selector):
        return {"voice_id": selector, "name": selector, "public_owner_id": None}
    raise ValueError(
        f"Unknown voice {selector!r} — use /voice voices to see the list, "
        "or pass a raw ElevenLabs voice ID"
    )


def set_voice(selector: str) -> Dict[str, Any]:
    """Swap the active ElevenLabs voice. Returns the resolved voice entry."""
    voice = resolve_voice(selector)
    vid = voice["voice_id"]

    # Shared-library voices must be claimed into the account first.
    if voice.get("public_owner_id"):
        _ensure_in_library(voice)

    import yaml

    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".config.yaml.lock"

    # Serialize the read-modify-write across processes (flock) and write
    # atomically (temp file + rename) so concurrent /voice set calls or
    # other config writers can't interleave or leave a torn file.
    lock_fh = open(lock_path, "w")
    try:
        if fcntl is not None:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        tts = cfg.setdefault("tts", {})
        if not isinstance(tts, dict):
            tts = cfg["tts"] = {}
        tts["provider"] = "elevenlabs"
        el = tts.setdefault("elevenlabs", {})
        if not isinstance(el, dict):
            el = tts["elevenlabs"] = {}
        el["voice_id"] = vid
        # Keep the alternate nested form in sync if the config uses it.
        providers = tts.get("providers")
        if isinstance(providers, dict) and isinstance(providers.get("elevenlabs"), dict):
            providers["elevenlabs"]["voice_id"] = vid

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(yaml.safe_dump(cfg, sort_keys=False))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fh.close()

    # Legacy adapter path reads this env var per TTS call.
    os.environ["ELEVENLABS_VOICE_ID"] = vid
    logger.info("Voice swapped to %s (%s)", voice.get("name"), vid)
    return voice


def format_voice_list() -> str:
    """Human-readable numbered list of the top voices for chat output."""
    voices = fetch_top_female_voices()
    current = get_current_voice_id()
    lines = ["🎙️ **Top adult female ElevenLabs voices** — swap with `/voice set <number|name|id>`", ""]
    for i, v in enumerate(voices, 1):
        marker = " ← current" if v["voice_id"] == current else ""
        bits = [b for b in (v.get("accent"), v.get("age"), v.get("use_case")) if b]
        detail = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"`{i:2d}.` **{v['name']}**{detail} — `{v['voice_id']}`{marker}")
    if current and all(v["voice_id"] != current for v in voices):
        lines.append(f"\nCurrent voice: `{current}` (not in this list)")
    return "\n".join(lines)
