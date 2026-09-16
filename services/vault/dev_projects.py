"""Validation and lookup helpers for dev-request destinations.

The dispatch registry is deliberately a small Redis value rather than a
second database table.  Keeping its parsing here gives the vault and the
agent-side request tool the same naming and error semantics, including when
this module is imported from the standalone vault process (where
``services`` is not necessarily a Python package).
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

K_PROJECTS = "replitmcp:projects"
K_TARGET = "replitmcp:target_repl"
DEFAULT_PROJECT = "open-manus"

# These limits are intentionally conservative: project names and repl IDs are
# operator configuration, not arbitrary request payloads.  They also keep
# diagnostics and MCP arguments bounded if Redis is edited manually.
MAX_PROJECTS = 100
PROJECT_NAME_MAX = 64
REPL_ID_MAX = 128

_PROJECT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REPL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def normalize_project(value: Any) -> str:
    """Return the canonical destination name for *value*.

    Empty values mean the default ``open-manus`` destination.  Non-empty
    names are trimmed, lower-cased, and runs of whitespace become one
    hyphen.  Punctuation is not silently discarded: callers should use
    :func:`validate_projects` when accepting a registry update so malformed
    names produce an actionable error.
    """
    if value is None:
        return DEFAULT_PROJECT
    if not isinstance(value, str):
        raise ValueError("project name must be a string")
    if len(value) > 256:
        raise ValueError("project name input is too long (maximum 256 characters)")
    name = re.sub(r"\s+", "-", value.strip().lower())
    name = name or DEFAULT_PROJECT
    if len(name) > PROJECT_NAME_MAX or not _PROJECT_RE.fullmatch(name):
        raise ValueError(
            "invalid project name; use letters, numbers, and single hyphens "
            f"(maximum {PROJECT_NAME_MAX} characters)")
    return name


def _validate_name(raw_name: Any) -> str:
    name = normalize_project(raw_name)
    if len(name) > PROJECT_NAME_MAX:
        raise ValueError(
            f"project name '{name[:32]}…' is too long "
            f"(maximum {PROJECT_NAME_MAX} characters)")
    if not _PROJECT_RE.fullmatch(name):
        raise ValueError(
            f"invalid project name '{name[:64]}'; use lowercase letters, "
            "numbers, and single hyphens only")
    return name


def _validate_repl_id(raw_id: Any, name: str) -> str:
    if not isinstance(raw_id, str):
        raise ValueError(f"repl ID for project '{name}' must be a string")
    repl_id = raw_id.strip()
    if not repl_id:
        raise ValueError(f"repl ID for project '{name}' cannot be blank")
    if len(repl_id) > REPL_ID_MAX:
        raise ValueError(
            f"repl ID for project '{name}' is too long "
            f"(maximum {REPL_ID_MAX} characters)")
    if not _REPL_ID_RE.fullmatch(repl_id):
        raise ValueError(
            f"invalid repl ID for project '{name}'; use letters, numbers, "
            "hyphens, and underscores only")
    return repl_id


def validate_projects(mapping: Mapping[Any, Any]) -> dict[str, str]:
    """Validate and canonicalize a project-name → repl-ID mapping.

    The returned mapping is a fresh dictionary safe to serialize to Redis.
    Canonicalization collisions are rejected rather than allowing the last
    value in a JSON object to silently win.
    """
    if not isinstance(mapping, Mapping):
        raise ValueError("project registry must be a JSON object (name → repl ID)")
    if len(mapping) > MAX_PROJECTS:
        raise ValueError(
            f"project registry has too many projects (maximum {MAX_PROJECTS})")

    result: dict[str, str] = {}
    id_owners: dict[str, str] = {}
    for raw_name, raw_id in mapping.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("configured project names cannot be blank")
        name = _validate_name(raw_name)
        if name in result:
            raise ValueError(
                f"project names collide after canonicalization: '{name}'")
        repl_id = _validate_repl_id(raw_id, name)
        if repl_id in id_owners:
            raise ValueError(
                f"repl ID is assigned to more than one project: "
                f"'{id_owners[repl_id]}' and '{name}'")
        id_owners[repl_id] = name
        result[name] = repl_id
    return result


def _read_json_mapping(raw) -> dict[str, str]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "stored project registry is not valid JSON; save a JSON object "
            "mapping project names to repl IDs") from exc
    return validate_projects(parsed)


def _read_legacy_default(raw) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if raw in (None, ""):
        return ""
    # Reuse the same repl-ID constraints, but never expose a legacy value as
    # a project other than the default destination.
    return _validate_repl_id(str(raw), DEFAULT_PROJECT)


def _read_snapshot(r) -> tuple[dict[str, str], str]:
    """Read the registry and legacy default in one atomic Redis MGET.

    Resolving a destination must not observe a registry from one update and a
    default target from another.  MGET is one Redis command, so both values
    come from the same point-in-time snapshot.
    """
    values = r.mget([K_PROJECTS, K_TARGET])
    if len(values) != 2:  # pragma: no cover - defensive for non-Redis clients
        values = list(values) + [None] * (2 - len(values))
    return _read_json_mapping(values[0]), _read_legacy_default(values[1])


def list_projects(r) -> dict[str, str]:
    """Return the explicit project registry, excluding the legacy key."""
    projects, _legacy = _read_snapshot(r)
    return projects


def _legacy_default(r) -> str:
    _projects, legacy = _read_snapshot(r)
    return legacy


def resolve_project(r, value: Any) -> tuple[str, str]:
    """Resolve a requested destination to ``(canonical_name, repl_id)``.

    The legacy target key is intentionally consulted only for ``open-manus``.
    A configured default in the new registry wins over that compatibility
    fallback.  Unknown names are errors rather than accidental default
    dispatches.
    """
    name = _validate_name(value)
    # Keep the explicit registry and compatibility default from one atomic
    # snapshot.  In particular, do not read the two keys independently while
    # an administrator is updating the destination configuration.
    projects, legacy = _read_snapshot(r)
    if name in projects:
        return name, projects[name]
    if name == DEFAULT_PROJECT:
        if legacy:
            return name, legacy
    names = set(projects)
    if DEFAULT_PROJECT not in names and legacy:
        names.add(DEFAULT_PROJECT)
    available = ", ".join(sorted(names))
    if available:
        raise ValueError(
            f"unknown project '{name}'; choose one of the configured "
            f"destinations: {available}")
    raise ValueError(
        f"project '{name}' is not configured; configure a repl ID for "
        f"'{name}' in the vault dashboard before retrying")


def configured_names(r) -> list[str]:
    """Return configured destination names only, in stable order."""
    projects, legacy = _read_snapshot(r)
    names = set(projects)
    # open-manus is available through the compatibility key only when the
    # legacy key has a value; it must not appear as a phantom option.
    if DEFAULT_PROJECT not in names and legacy:
        names.add(DEFAULT_PROJECT)
    return sorted(names)


__all__ = [
    "DEFAULT_PROJECT",
    "K_PROJECTS",
    "K_TARGET",
    "MAX_PROJECTS",
    "PROJECT_NAME_MAX",
    "REPL_ID_MAX",
    "configured_names",
    "list_projects",
    "normalize_project",
    "resolve_project",
    "validate_projects",
]