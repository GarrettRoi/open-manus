"""/topup — OpenRouter credit balance + checkout link.

Purely programmatic (no agent/LLM in the loop): the handler calls the
OpenRouter API directly with the account-level key and formats the reply.

OpenRouter removed programmatic credit *purchases* (the old
``POST /api/v1/credits/coinbase`` endpoint returns 410 Gone), so actual
payment happens through their web checkout — this command gets you one
click away from it with the live balance in front of you.

Key resolution order:
    1. OPENROUTER_MANAGEMENT_KEY  (account-level / provisioning key —
       enables the exact purchased/used totals from /api/v1/credits)
    2. OPENROUTER_API_KEY         (regular inference key — falls back to
       /api/v1/auth/key which reports usage and limit)
"""

from __future__ import annotations

import json
import os
from typing import Optional
from urllib.request import Request, urlopen

CREDITS_URL = "https://openrouter.ai/api/v1/credits"
AUTH_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
CHECKOUT_URL = "https://openrouter.ai/settings/credits"


def _get(url: str, key: str, timeout: float = 15) -> dict:
    req = Request(url, headers={"Authorization": f"Bearer {key}",
                                "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fmt_usd(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "?"


def _balance_via_credits(key: str) -> Optional[str]:
    """Account-level endpoint: total purchased and used."""
    data = (_get(CREDITS_URL, key) or {}).get("data") or {}
    if "total_credits" not in data and "total_usage" not in data:
        return None
    purchased = float(data.get("total_credits") or 0.0)
    used = float(data.get("total_usage") or 0.0)
    remaining = purchased - used
    warn = "  ⚠️ LOW" if remaining < 5 else ""
    return (
        f"**OpenRouter credits**\n"
        f"• Remaining: **{_fmt_usd(remaining)}**{warn}\n"
        f"• Purchased: {_fmt_usd(purchased)}  |  Used: {_fmt_usd(used)}\n\n"
        f"💳 Buy more: {CHECKOUT_URL}\n"
        f"(OpenRouter has no purchase API — checkout completes in the browser; "
        f"enable auto top-up on that page for hands-off refills.)"
    )


def _balance_via_auth_key(key: str) -> Optional[str]:
    """Regular-key fallback: usage and limit for this key."""
    data = (_get(AUTH_KEY_URL, key) or {}).get("data") or {}
    if not data:
        return None
    usage = data.get("usage")
    limit = data.get("limit")
    lines = ["**OpenRouter key status**"]
    if limit is not None:
        remaining = float(limit) - float(usage or 0.0)
        warn = "  ⚠️ LOW" if remaining < 5 else ""
        lines.append(f"• Remaining on this key: **{_fmt_usd(remaining)}**{warn}")
        lines.append(f"• Limit: {_fmt_usd(limit)}  |  Used: {_fmt_usd(usage)}")
    else:
        lines.append(f"• Used via this key: {_fmt_usd(usage)} (no key limit set)")
        lines.append("• For the account-wide balance, set OPENROUTER_MANAGEMENT_KEY "
                     "(a provisioning key from openrouter.ai/settings/provisioning-keys).")
    lines.append(f"\n💳 Buy more: {CHECKOUT_URL}\n"
                 f"(OpenRouter has no purchase API — checkout completes in the "
                 f"browser; enable auto top-up on that page for hands-off refills.)")
    return "\n".join(lines)


def _handle_slash(raw_args: str) -> Optional[str]:
    mgmt_key = (os.getenv("OPENROUTER_MANAGEMENT_KEY") or "").strip()
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not mgmt_key and not api_key:
        return ("No OpenRouter key configured. Set OPENROUTER_MANAGEMENT_KEY "
                "(preferred, account-level) or OPENROUTER_API_KEY.")

    errors = []
    for key, fn, label in (
        (mgmt_key, _balance_via_credits, "credits endpoint"),
        (api_key, _balance_via_credits, "credits endpoint (api key)"),
        (api_key or mgmt_key, _balance_via_auth_key, "auth/key endpoint"),
    ):
        if not key:
            continue
        try:
            out = fn(key)
            if out:
                return out
        except Exception as e:  # noqa: BLE001 — report, never crash dispatch
            errors.append(f"{label}: {e}")

    detail = ("; ".join(errors))[:300] or "no data returned"
    return (f"Couldn't fetch the OpenRouter balance ({detail}).\n"
            f"💳 Checkout link still works: {CHECKOUT_URL}")


def register(ctx) -> None:
    ctx.register_command(
        "topup",
        handler=_handle_slash,
        description="OpenRouter credit balance + link to buy more credits.",
    )
