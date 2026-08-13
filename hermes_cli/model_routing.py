"""Per-agent task→model routing.

An agent's ``routing:`` config section maps task categories to cheaper
models so background work (cron polling, delegated subagent chores,
auxiliary side tasks) doesn't burn the flagship primary model:

    routing:
      cron_job: deepseek/deepseek-chat
      background_task: google/gemini-2.5-flash-preview
      # dict form when a category needs its own provider:
      code: {model: minimax/minimax-m2.5, provider: openrouter}

Resolution precedence everywhere routing is consulted:

    explicit pin (per-job model / delegation.model / auxiliary.<task>.model)
        > routing rule for the category (with category fallbacks)
        > primary model (existing behaviour, untouched)

Anything unspecified falls through to the primary model — an empty or
absent ``routing`` section is a no-op. This module is the single source
of truth for the category list (it replaces the old unwired
``deploy/shared/model_router.py`` catalog).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Canonical routing categories. Unknown keys in a user's routing config are
# still honoured on direct lookup (forward compatibility) but warned about
# once so typos ("cronjob" vs "cron_job") don't fail silently.
ROUTING_CATEGORIES = (
    "cron_job",          # scheduled cron jobs (unpinned)
    "background_task",   # generic cheap background work
    "quick_response",    # short/cheap interactive side calls
    "code",              # code generation / engineering work
    "chat",              # main conversational traffic (rarely set; primary wins)
    "delegation",        # delegated subagents without an explicit delegation.model
    "multimodal",        # vision / image analysis auxiliary tasks
)

# When a category has no rule, these are tried in order before giving up.
CATEGORY_FALLBACKS: Dict[str, tuple] = {
    "cron_job": ("background_task",),
    "delegation": ("background_task",),
    "quick_response": ("background_task",),
    "multimodal": (),
    "background_task": (),
    "code": (),
    "chat": (),
}

# Auxiliary task name → routing category. Any auxiliary task not listed
# here falls back to ``background_task``.
AUX_TASK_CATEGORIES: Dict[str, str] = {
    "vision": "multimodal",
    "browser_vision": "multimodal",
    "image_analysis": "multimodal",
}

_warned_unknown: set = set()


def _normalize_rule(category: str, value: Any) -> Optional[Dict[str, str]]:
    """Coerce a routing rule value into ``{"model": ..., "provider": ...}``.

    Accepts a bare model string or a ``{model, provider}`` dict. Returns
    ``None`` (with a warning) for anything unusable so a bad entry degrades
    to the primary model instead of shipping garbage to a provider.
    """
    if isinstance(value, str):
        model = value.strip()
        if not model or model.lower() == "auto":
            return None
        return {"model": model, "provider": ""}
    if isinstance(value, dict):
        model = str(value.get("model") or "").strip()
        provider = str(value.get("provider") or "").strip()
        if not model:
            logger.warning("routing.%s has no 'model' — ignoring rule", category)
            return None
        return {"model": model, "provider": provider}
    if value not in (None, ""):
        logger.warning(
            "routing.%s has unsupported value %r — expected a model string "
            "or {model, provider} dict; ignoring rule", category, value,
        )
    return None


def normalize_routing_config(raw: Any) -> Dict[str, Dict[str, str]]:
    """Validate/normalize a raw ``routing`` config section.

    Returns ``{category: {"model": str, "provider": str}}`` with invalid
    entries dropped. Unknown categories are kept (forward compatible) but
    warned about once per process.
    """
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, Dict[str, str]] = {}
    for key, value in raw.items():
        category = str(key or "").strip()
        if not category:
            continue
        if category not in ROUTING_CATEGORIES and category not in _warned_unknown:
            _warned_unknown.add(category)
            logger.warning(
                "routing.%s is not a known category (known: %s) — it is kept "
                "but only used by direct lookups", category,
                ", ".join(ROUTING_CATEGORIES),
            )
        rule = _normalize_rule(category, value)
        if rule:
            normalized[category] = rule
    return normalized


def resolve_routed_model(
    cfg: Optional[Dict[str, Any]],
    category: str,
) -> Optional[Dict[str, str]]:
    """Resolve a routing rule for ``category`` from a loaded config dict.

    Tries the category itself, then its ``CATEGORY_FALLBACKS`` chain.
    Returns ``{"model": str, "provider": str}`` or ``None`` when no rule
    applies (caller keeps its existing/primary model).
    """
    if not isinstance(cfg, dict):
        return None
    routing = normalize_routing_config(cfg.get("routing"))
    if not routing:
        return None
    for candidate in (category, *CATEGORY_FALLBACKS.get(category, ())):
        rule = routing.get(candidate)
        if rule:
            return dict(rule)
    return None


def resolve_routed_model_for_aux_task(
    cfg: Optional[Dict[str, Any]],
    task: str,
) -> Optional[Dict[str, str]]:
    """Routing rule for an auxiliary task name (vision → multimodal, etc.)."""
    category = AUX_TASK_CATEGORIES.get(str(task or "").strip(), "background_task")
    return resolve_routed_model(cfg, category)
