#!/usr/bin/env python3
"""
Apply Redis-persisted per-agent runtime settings at container startup.

Some settings are changed at runtime (via slash commands) and must survive
redeploys, but their on-disk homes get re-baked from the image before the
memory restore runs (which never overwrites existing files):

  - Voice character:  /voice-character writes tts.elevenlabs.voice_id into
    ~/.hermes/config.yaml — but entrypoint.sh copies deploy/<agent>/config.yaml
    over it on every deploy.
  - Home voice channel:  /voicehome writes ~/.hermes/discord_home_voice.json —
    a fresh container simply doesn't have the file.

So the runtime setters ALSO mirror the values to Redis:

    agent:{name}:settings:voice_id            -> "<elevenlabs voice id>"
    agent:{name}:settings:home_voice_channel  -> '{"channel_id": 123}' or
                                                 '{"channel_id": null}' (cleared)

and this script (run by entrypoint.sh AFTER config.yaml is staged, BEFORE the
gateway starts) re-applies them to the freshly-baked files so every consumer
sees the user's last choice without any runtime Redis reads.

Usage:
    python3 apply_agent_settings.py --agent samantha
"""
import argparse
import json
import os
import sys
from pathlib import Path

REDIS_URL = os.getenv("REDIS_URL", "")
HERMES_HOME = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


def settings_key(agent: str, name: str) -> str:
    return f"agent:{agent.lower()}:settings:{name}"


def _apply_voice_id(vid: str) -> None:
    import yaml

    path = HERMES_HOME / "config.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        print(f"[agent-settings] config.yaml is not a mapping — skipping voice_id", file=sys.stderr)
        return
    tts = cfg.setdefault("tts", {})
    if not isinstance(tts, dict):
        tts = cfg["tts"] = {}
    tts["provider"] = "elevenlabs"
    el = tts.setdefault("elevenlabs", {})
    if not isinstance(el, dict):
        el = tts["elevenlabs"] = {}
    if el.get("voice_id") == vid:
        print(f"[agent-settings] voice_id already {vid} — nothing to do")
        return
    el["voice_id"] = vid
    providers = tts.get("providers")
    if isinstance(providers, dict) and isinstance(providers.get("elevenlabs"), dict):
        providers["elevenlabs"]["voice_id"] = vid

    import yaml as _yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(_yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    print(f"[agent-settings] Restored voice character {vid} into config.yaml")


def _apply_home_voice(raw: str) -> None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "channel_id" not in data:
            raise ValueError("bad payload")
    except ValueError:
        print(f"[agent-settings] Ignoring malformed home_voice_channel value: {raw!r}",
              file=sys.stderr)
        return
    path = HERMES_HOME / "discord_home_voice.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"channel_id": data["channel_id"]}), encoding="utf-8")
    tmp.replace(path)
    print(f"[agent-settings] Restored home voice channel: {data['channel_id']}")


def _apply_model(raw: str) -> None:
    """Apply a persisted per-agent model override to config.yaml.

    Reads the JSON payload from Redis key ``agent:{name}:settings:model``
    and writes ``model.default`` / ``model.provider`` (and optionally
    ``model.base_url``) into ``~/.hermes/config.yaml`` so the gateway
    starts with the user's last chosen model rather than the baked-in
    deploy config.  The write is atomic (temp-file replace).
    """
    import yaml

    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "model" not in data:
            raise ValueError("bad payload")
        model = str(data.get("model", "")).strip()
        provider = str(data.get("provider", "openrouter")).strip()
        base_url = str(data.get("base_url", "")).strip()
    except ValueError:
        print(
            f"[agent-settings] Ignoring malformed model value: {raw!r}",
            file=sys.stderr,
        )
        return

    if not model:
        print("[agent-settings] model value is empty — skipping", file=sys.stderr)
        return

    path = HERMES_HOME / "config.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        print("[agent-settings] config.yaml is not a mapping — skipping model", file=sys.stderr)
        return

    raw_model = cfg.get("model")
    if isinstance(raw_model, dict):
        model_cfg = raw_model
    elif isinstance(raw_model, str) and raw_model.strip():
        model_cfg = {"default": raw_model.strip()}
        cfg["model"] = model_cfg
    else:
        model_cfg = {}
        cfg["model"] = model_cfg

    if model_cfg.get("default") == model and model_cfg.get("provider") == provider:
        print(f"[agent-settings] model already {model} ({provider}) — nothing to do")
        return

    model_cfg["default"] = model
    model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    elif "base_url" in model_cfg and provider.lower() != "custom":
        # Remove stale base_url when switching back to a hosted provider.
        model_cfg.pop("base_url", None)

    import yaml as _yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(_yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    print(f"[agent-settings] Restored model {model!r} (provider: {provider}) into config.yaml")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    args = ap.parse_args()

    if not REDIS_URL:
        print("[agent-settings] REDIS_URL not set — nothing to restore")
        return 0
    try:
        import redis
        r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10)
        vid = r.get(settings_key(args.agent, "voice_id"))
        home = r.get(settings_key(args.agent, "home_voice_channel"))
        model_raw = r.get(settings_key(args.agent, "model"))
    except Exception as e:
        print(f"[agent-settings] Redis unavailable ({e}) — skipping", file=sys.stderr)
        return 0

    if vid and vid.strip():
        try:
            _apply_voice_id(vid.strip())
        except Exception as e:
            print(f"[agent-settings] Failed to apply voice_id: {e}", file=sys.stderr)
    if home:
        try:
            _apply_home_voice(home)
        except Exception as e:
            print(f"[agent-settings] Failed to apply home_voice_channel: {e}", file=sys.stderr)
    if model_raw:
        try:
            _apply_model(model_raw)
        except Exception as e:
            print(f"[agent-settings] Failed to apply model: {e}", file=sys.stderr)
    if not vid and not home and not model_raw:
        print("[agent-settings] No persisted settings for this agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
