"""Bounded web-provider selection and response classification.

No network calls or browser session mutation live here.
"""

import re

PREFERENCE = (
    "vault_keenable", "vault_firecrawl", "firecrawl", "tavily", "exa", "parallel",
    "searxng", "brave-free", "ddgs",
)
MAX_ATTEMPTS = 3
_FREE_BACKENDS = frozenset({"searxng", "brave-free", "ddgs"})
NEXT_STEP = (
    "Try browser_navigate with the public URL, then browser_snapshot "
    "to inspect the page (or open the URL manually and provide its text). "
    "For a login or CAPTCHA, complete it manually in the browser session first."
)


def candidates(primary, get_provider, list_providers, capability, active=None):
    """Keep the chosen provider first, then available registered alternatives."""
    result = []
    seen = set()
    seen_providers = set()
    names = [primary]
    if active is not None:
        names.append(getattr(active, "name", None))
    names.extend(PREFERENCE)
    try:
        names.extend(p.name for p in list_providers())
    except Exception:
        pass
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        # A Keenable pro request must not silently degrade to a free/public
        # search backend. Explicitly selecting a free backend still works.
        if primary == "vault_keenable" and name in _FREE_BACKENDS:
            continue
        try:
            provider = get_provider(name)
            if provider is None or not getattr(provider, f"supports_{capability}")():
                continue
            if provider.name in seen_providers:
                continue
            # Explicit selection retains its legacy behavior even when the
            # provider reports unavailable (it can return a setup error).
            if name != primary and (active is None or name != getattr(active, "name", None)):
                if not provider.is_available():
                    continue
            result.append(provider)
            seen_providers.add(provider.name)
        except Exception:
            continue
    return result[:MAX_ATTEMPTS]


def terminal_error(item):
    """Policy and interruption failures must never be sent to another reader."""
    error = str(item.get("error") or "").lower()
    return bool(item.get("blocked_by_policy")) or error.startswith("blocked:") or (
        "blocked by website policy" in error
        or "blocked by website access policy" in error
        or "private or internal network" in error
        or error == "interrupted"
    )


_CHALLENGE = re.compile(
    r"(?:captcha|verify you are human|checking your browser|"
    r"just a moment\.\.\.|enable javascript and cookies to continue|"
    r"access denied|cloudflare ray id|"
    r"attention required! \| cloudflare)",
    re.I,
)


def content_problem(content):
    """Only short challenge/error pages are treated as blocked, not articles."""
    if not isinstance(content, str) or not content.strip():
        return "empty content"
    text = content.strip()
    if len(text) < 1200 and _CHALLENGE.search(text):
        # Articles mentioning CAPTCHA in passing are not challenge pages.
        if re.search(
            r"(?:solve the captcha|complete the captcha|"
            r"verify you are human|checking your browser|"
            r"enable javascript and cookies to continue|"
            r"cloudflare ray id|attention required! \| cloudflare|"
            r"^access denied\b|^captcha\s*$)",
            text, re.I,
        ):
            return "challenge or access-denied page"
    return None