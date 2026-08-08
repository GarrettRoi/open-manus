"""Regression tests for the Discord /model picker.

Uses the shared discord mock from tests/gateway/conftest.py (installed
at collection time via _ensure_discord_mock()). Previously this file
installed its own mock at module-import time and clobbered sys.modules,
breaking other gateway tests under pytest-xdist.
"""

import json
import os
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.discord.adapter import ModelPickerView


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_interaction(model_value: str, user_id=123, channel_id=456):
    """Build a minimal Discord Interaction stub for model-select tests."""
    edit_message_events: list = []
    edit_original_events: list = []

    async def _edit_message(**kwargs):
        edit_message_events.append(kwargs)

    async def _edit_original(**kwargs):
        edit_original_events.append(kwargs)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        channel_id=channel_id,
        data={"values": [model_value]},
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit_message),
        ),
        edit_original_response=AsyncMock(side_effect=_edit_original),
        _edit_message_events=edit_message_events,
        _edit_original_events=edit_original_events,
    )
    return interaction


def _sample_catalog():
    """Return a small OpenRouter-by-author catalog for picker tests."""
    return {
        "anthropic": [
            ("anthropic/claude-3-5-sonnet-20241022", ""),
            ("anthropic/claude-3-opus-20240229", ""),
            ("anthropic/claude-3-haiku-20240307", ""),
        ],
        "openai": [
            ("openai/gpt-4o", ""),
            ("openai/gpt-4-turbo", ""),
        ],
        "google": [
            ("google/gemini-pro-1.5", ""),
        ],
    }


# ---------------------------------------------------------------------------
# Existing regression tests (unchanged)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_picker_clears_controls_before_running_switch_callback():
    events: list[object] = []

    async def on_model_selected(chat_id: str, model_id: str, provider_slug: str) -> str:
        events.append(("switch", chat_id, model_id, provider_slug))
        return "Model switched"

    async def edit_message(**kwargs):
        events.append(
            (
                "initial-edit",
                kwargs["embed"].title,
                kwargs["embed"].description,
                kwargs["view"],
            )
        )

    async def edit_original_response(**kwargs):
        events.append((
            "final-edit",
            kwargs["embed"].title,
            kwargs["embed"].description,
            kwargs["view"],
        ))

    view = ModelPickerView(
        providers=[
            {
                "slug": "copilot",
                "name": "GitHub Copilot",
                "models": ["gpt-5.4"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model="gpt-5-mini",
        current_provider="copilot",
        session_key="session-1",
        on_model_selected=on_model_selected,
        allowed_user_ids={"123"},  # matches the interaction user; empty = fail-closed
    )
    view._selected_provider = "copilot"

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        channel_id=456,
        data={"values": ["gpt-5.4"]},
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=edit_message),
        ),
        edit_original_response=AsyncMock(side_effect=edit_original_response),
    )

    await view._on_model_selected(interaction)

    assert events == [
        ("initial-edit", "⚙ Switching Model", "Switching to `gpt-5.4`...", None),
        ("switch", "456", "gpt-5.4", "copilot"),
        ("final-edit", "⚙ Model Switched", "Model switched", None),
    ]
    interaction.response.edit_message.assert_awaited_once()
    interaction.response.defer.assert_not_called()
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_expensive_model_requires_confirmation(monkeypatch):
    events: list[object] = []

    async def on_model_selected(chat_id: str, model_id: str, provider_slug: str) -> str:
        events.append(("switch", chat_id, model_id, provider_slug))
        return "Model switched"

    async def edit_message(**kwargs):
        events.append(
            (
                "edit",
                kwargs["embed"].title,
                kwargs["embed"].description,
                kwargs["view"],
            )
        )

    async def edit_original_response(**kwargs):
        events.append((
            "final-edit",
            kwargs["embed"].title,
            kwargs["embed"].description,
            kwargs["view"],
        ))

    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.expensive_model_warning",
        lambda *_args, **_kwargs: SimpleNamespace(
            message="!!! EXPENSIVE MODEL WARNING !!!\ndid you mean to select openai/gpt-5.5?"
        ),
    )

    view = ModelPickerView(
        providers=[
            {
                "slug": "openrouter",
                "name": "OpenRouter",
                "models": ["openai/gpt-5.5-pro"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model="openai/gpt-5.5",
        current_provider="openrouter",
        session_key="session-1",
        on_model_selected=on_model_selected,
        allowed_user_ids={"123"},  # matches the interaction user; empty = fail-closed
    )
    view._selected_provider = "openrouter"

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        channel_id=456,
        data={"values": ["openai/gpt-5.5-pro"]},
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=edit_message),
        ),
        edit_original_response=AsyncMock(side_effect=edit_original_response),
    )

    await view._on_model_selected(interaction)

    assert events == [
        (
            "edit",
            "⚠ Expensive Model Warning",
            "!!! EXPENSIVE MODEL WARNING !!!\ndid you mean to select openai/gpt-5.5?",
            view,
        ),
    ]
    assert view.resolved is False

    await view._on_expensive_confirm(interaction)

    assert events[1:] == [
        (
            "edit",
            "⚙ Switching Model",
            "Switching to `openai/gpt-5.5-pro`...",
            None,
        ),
        ("switch", "456", "openai/gpt-5.5-pro", "openrouter"),
        ("final-edit", "⚙ Model Switched", "Model switched", None),
    ]


# ---------------------------------------------------------------------------
# New: catalog grouping helpers
# ---------------------------------------------------------------------------

def test_fetch_openrouter_catalog_by_author_fallback_on_error(monkeypatch):
    """When the HTTP fetch fails, _fallback() groups OPENROUTER_MODELS by prefix."""
    import urllib.request

    import hermes_cli.models as _m

    # Reset module-level cache so this test is isolated.
    _m._openrouter_author_catalog_cache = None
    _m._openrouter_author_catalog_ts = 0.0

    def _raise(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    catalog = _m.fetch_openrouter_catalog_by_author()

    # Must return something (the curated fallback), not raise or return empty.
    assert isinstance(catalog, dict)
    assert len(catalog) > 0
    # All values must be non-empty lists of (model_id, desc) tuples.
    for author, models in catalog.items():
        assert isinstance(author, str) and author
        assert isinstance(models, list) and models
        assert all(isinstance(mid, str) and "/" in mid for mid, _ in models)


def test_fetch_openrouter_catalog_by_author_groups_live_data(monkeypatch):
    """Live fetch is parsed into author groups with tool-filter applied."""
    import urllib.request
    from io import BytesIO
    import hermes_cli.models as _m

    _m._openrouter_author_catalog_cache = None
    _m._openrouter_author_catalog_ts = 0.0

    fake_payload = json.dumps({
        "data": [
            {
                "id": "anthropic/claude-3-5-sonnet",
                "supported_parameters": ["tools", "temperature"],
                "pricing": {"prompt": "0.003", "completion": "0.015"},
            },
            {
                "id": "openai/gpt-4o",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.005", "completion": "0.015"},
            },
            # Should be filtered out — no tools in supported_parameters.
            {
                "id": "somemodel/no-tools",
                "supported_parameters": ["temperature"],
                "pricing": {"prompt": "0", "completion": "0"},
            },
            # Free model.
            {
                "id": "openai/gpt-4o-mini",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0", "completion": "0"},
            },
            # No slash → skip.
            {"id": "no-slash-model", "supported_parameters": ["tools"]},
        ]
    }).encode()

    class _FakeResp:
        def read(self):
            return fake_payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResp())

    catalog = _m.fetch_openrouter_catalog_by_author()

    assert "anthropic" in catalog
    assert "openai" in catalog
    assert "somemodel" not in catalog  # filtered — no tools
    assert len(catalog["anthropic"]) == 1
    assert catalog["anthropic"][0][0] == "anthropic/claude-3-5-sonnet"

    openai_ids = [mid for mid, _ in catalog["openai"]]
    assert "openai/gpt-4o" in openai_ids
    assert "openai/gpt-4o-mini" in openai_ids
    # Free model marked as "free" in desc
    mini = next((d for mid, d in catalog["openai"] if mid == "openai/gpt-4o-mini"), None)
    assert mini == "free"


def test_fetch_openrouter_catalog_by_author_ttl_cache(monkeypatch):
    """Repeated calls within the TTL do not re-issue the HTTP request."""
    import time
    import urllib.request
    import hermes_cli.models as _m

    call_count = 0

    class _FakeResp:
        def read(self):
            return json.dumps({"data": [
                {"id": "x/y", "supported_parameters": ["tools"],
                 "pricing": {"prompt": "0", "completion": "0"}},
            ]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def _urlopen(*a, **kw):
        nonlocal call_count
        call_count += 1
        return _FakeResp()

    _m._openrouter_author_catalog_cache = None
    _m._openrouter_author_catalog_ts = 0.0
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    _m.fetch_openrouter_catalog_by_author()
    _m.fetch_openrouter_catalog_by_author()  # should hit cache
    assert call_count == 1

    # force_refresh bypasses TTL.
    _m.fetch_openrouter_catalog_by_author(force_refresh=True)
    assert call_count == 2


def test_sort_authors_for_picker_priority_ordering():
    """Priority authors come first in defined order; rest sorted by model count."""
    from hermes_cli.models import sort_authors_for_picker

    catalog = {
        "anthropic": [("a/m", "")] * 5,
        "google": [("g/m", "")] * 10,
        "openai": [("o/m", "")] * 3,
        "unknown-co": [("u/m", "")] * 20,
        "deepseek": [("d/m", "")] * 2,
    }
    authors = list(catalog.keys())
    sorted_authors = sort_authors_for_picker(authors, catalog=catalog)

    # anthropic, openai, google, deepseek must appear before unknown-co in that order.
    idx = {a: sorted_authors.index(a) for a in sorted_authors}
    assert idx["anthropic"] < idx["google"]
    assert idx["openai"] < idx["google"]
    assert idx["deepseek"] < idx["unknown-co"]
    # unknown-co not in priority list → at end
    assert idx["unknown-co"] > idx["deepseek"]


def test_author_display_name_known_and_fallback():
    from hermes_cli.models import author_display_name

    assert author_display_name("anthropic") == "Anthropic"
    assert author_display_name("x-ai") == "xAI (Grok)"
    assert author_display_name("meta-llama") == "Meta (Llama)"
    # Unknown slug → capitalised words
    assert author_display_name("some-new-lab") == "Some New Lab"


# ---------------------------------------------------------------------------
# New: company-first picker — stage 1 (company select)
# ---------------------------------------------------------------------------

def test_model_picker_with_catalog_builds_company_select():
    """With openrouter_catalog, __init__ builds a company select, not provider select."""
    view = ModelPickerView(
        providers=[],
        current_model="anthropic/claude-3-5-sonnet-20241022",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
        openrouter_catalog=_sample_catalog(),
    )

    selects = [c for c in view.children if hasattr(c, "options") and hasattr(c, "placeholder")]
    assert selects, "Expected a Select component in the view"
    company_select = selects[0]
    assert "company" in (company_select.placeholder or "").lower()

    option_values = {o.value for o in company_select.options}
    assert "anthropic" in option_values
    assert "openai" in option_values
    assert "google" in option_values


def test_model_picker_without_catalog_builds_provider_select():
    """Without a catalog, __init__ builds the legacy provider select."""
    providers = [{"slug": "openrouter", "name": "OpenRouter",
                  "models": ["anthropic/claude-3"], "total_models": 1, "is_current": True}]
    view = ModelPickerView(
        providers=providers,
        current_model="anthropic/claude-3",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
    )

    selects = [c for c in view.children if hasattr(c, "options")]
    assert selects
    provider_select = selects[0]
    option_values = {o.value for o in provider_select.options}
    assert "openrouter" in option_values


def test_model_picker_company_select_marks_current_company():
    """The company owning the current model is annotated as 'current'."""
    view = ModelPickerView(
        providers=[],
        current_model="openai/gpt-4o",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
        openrouter_catalog=_sample_catalog(),
    )

    selects = [c for c in view.children if hasattr(c, "options")]
    company_select = selects[0]
    current_options = [o for o in company_select.options if o.description == "current"]
    assert len(current_options) == 1
    assert current_options[0].value == "openai"


def test_model_picker_company_select_paging():
    """When more than 25 companies exist, paging buttons appear."""
    # Build a catalog with 30 companies.
    big_catalog = {f"company-{i}": [(f"company-{i}/model-a", "")] for i in range(30)}
    view = ModelPickerView(
        providers=[],
        current_model="",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
        openrouter_catalog=big_catalog,
    )

    buttons = [c for c in view.children if hasattr(c, "label") and not hasattr(c, "options")]
    button_labels = {b.label for b in buttons}

    # There should be a "Next →" button but no "← Prev" on page 0.
    assert "Next →" in button_labels
    assert "← Prev" not in button_labels
    assert "🔌 Other providers" in button_labels


# ---------------------------------------------------------------------------
# New: company-first picker — stage 2 (company selected → model select)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_company_selected_transitions_to_model_select():
    """Selecting a company replaces the company select with a model select."""
    switch_calls: list = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        switch_calls.append((model_id, provider_slug))
        return "switched"

    view = ModelPickerView(
        providers=[],
        current_model="",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=on_model_selected,
        allowed_user_ids={"999"},
        openrouter_catalog=_sample_catalog(),
    )

    edit_args = []

    async def _edit(**kwargs):
        edit_args.append(kwargs)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        channel_id=100,
        data={"values": ["anthropic"]},
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit),
        ),
        edit_original_response=AsyncMock(),
    )

    await view._on_company_selected(interaction)

    # The embed description should mention the company.
    assert edit_args, "edit_message was not called"
    desc = edit_args[0]["embed"].description
    assert "Anthropic" in desc or "anthropic" in desc.lower()

    # The view should now show a model select with anthropic models.
    selects = [c for c in view.children if hasattr(c, "options")]
    assert selects
    model_values = {o.value for o in selects[0].options}
    assert "anthropic/claude-3-5-sonnet-20241022" in model_values
    assert view._selected_provider == "openrouter"
    assert view._current_company == "anthropic"


@pytest.mark.asyncio
async def test_company_model_select_paging():
    """Company with >25 models shows Next/Prev page buttons."""
    # 30 models for 'big-co'.
    many_models = [(f"big-co/model-{i:03d}", "") for i in range(30)]
    catalog = {"big-co": many_models}

    view = ModelPickerView(
        providers=[],
        current_model="",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
        openrouter_catalog=catalog,
    )

    # Directly build model select for big-co, page 0.
    view._build_model_select_for_company("big-co", page=0)

    selects = [c for c in view.children if hasattr(c, "options")]
    buttons = [c for c in view.children if hasattr(c, "label") and not hasattr(c, "options")]
    button_labels = {b.label for b in buttons}

    # Page 0 of 2: Next → but no ← Prev.
    assert selects, "No model select built"
    assert len(selects[0].options) == 25, "Page 0 should show exactly 25 models"
    assert "Next →" in button_labels
    assert "← Prev" not in button_labels

    # Page 1: ← Prev but no Next →.
    view._build_model_select_for_company("big-co", page=1)
    selects2 = [c for c in view.children if hasattr(c, "options")]
    buttons2 = [c for c in view.children if hasattr(c, "label") and not hasattr(c, "options")]
    button_labels2 = {b.label for b in buttons2}

    assert len(selects2[0].options) == 5, "Page 1 should show remaining 5 models"
    assert "← Prev" in button_labels2
    assert "Next →" not in button_labels2


@pytest.mark.asyncio
async def test_full_cascade_company_to_model_switch():
    """End-to-end: company select → model select → model switch → confirmation."""
    switch_calls: list = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        switch_calls.append((model_id, provider_slug))
        return "Switched OK"

    view = ModelPickerView(
        providers=[],
        current_model="",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=on_model_selected,
        allowed_user_ids={"42"},
        openrouter_catalog=_sample_catalog(),
    )

    # --- Stage 1: select company ---
    stage1_edits = []

    async def _edit1(**kwargs):
        stage1_edits.append(kwargs)

    ia_company = SimpleNamespace(
        user=SimpleNamespace(id=42),
        channel_id=7,
        data={"values": ["openai"]},
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit1),
        ),
        edit_original_response=AsyncMock(),
    )
    await view._on_company_selected(ia_company)
    assert stage1_edits, "Stage 1 edit_message not called"

    # --- Stage 2: select model ---
    final_edits = []
    original_edits = []

    async def _edit2(**kwargs):
        final_edits.append(kwargs)

    async def _edit_orig(**kwargs):
        original_edits.append(kwargs)

    ia_model = SimpleNamespace(
        user=SimpleNamespace(id=42),
        channel_id=7,
        data={"values": ["openai/gpt-4o"]},
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit2),
        ),
        edit_original_response=AsyncMock(side_effect=_edit_orig),
    )
    await view._on_model_selected(ia_model)

    assert switch_calls == [("openai/gpt-4o", "openrouter")]
    assert original_edits, "Confirmation embed not sent"
    assert "Switched OK" in original_edits[0]["embed"].description


# ---------------------------------------------------------------------------
# New: "Other providers" button reaches legacy multi-provider picker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_other_providers_switches_to_provider_select():
    """Clicking 'Other providers' transitions to the legacy provider-select view."""
    providers = [
        {"slug": "nous", "name": "Nous Portal",
         "models": ["nous/model-1"], "total_models": 1, "is_current": False},
    ]
    view = ModelPickerView(
        providers=providers,
        current_model="anthropic/claude-3",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
        openrouter_catalog=_sample_catalog(),
    )

    edit_args = []

    async def _edit(**kw):
        edit_args.append(kw)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        channel_id=1,
        data={"values": []},
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit),
        ),
        edit_original_response=AsyncMock(),
    )
    await view._on_other_providers(interaction)

    assert view._in_other_providers_mode is True
    selects = [c for c in view.children if hasattr(c, "options")]
    assert selects
    provider_values = {o.value for o in selects[0].options}
    assert "nous" in provider_values


@pytest.mark.asyncio
async def test_back_from_model_select_returns_to_company_select():
    """Back button from model-select returns to company select (not provider select)."""
    view = ModelPickerView(
        providers=[],
        current_model="",
        current_provider="openrouter",
        session_key="s",
        on_model_selected=AsyncMock(),
        allowed_user_ids={"1"},
        openrouter_catalog=_sample_catalog(),
    )

    # Navigate to model select for anthropic.
    view._build_model_select_for_company("anthropic", page=0)

    edit_args = []

    async def _edit(**kw):
        edit_args.append(kw)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        channel_id=1,
        data={"values": []},
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit),
        ),
        edit_original_response=AsyncMock(),
    )
    await view._on_back(interaction)

    # Should have switched back to company select.
    selects = [c for c in view.children if hasattr(c, "options")]
    assert selects, "No select after Back"
    option_values = {o.value for o in selects[0].options}
    assert "anthropic" in option_values  # company select, not provider
    assert "openrouter" not in option_values  # not the provider picker


# ---------------------------------------------------------------------------
# New: Redis persistence via _persist_agent_model_to_redis
# ---------------------------------------------------------------------------

def test_persist_agent_model_to_redis_writes_correct_key(monkeypatch):
    """_persist_agent_model_to_redis writes JSON to the correct Redis key."""
    import gateway.slash_commands as sc

    written: dict = {}

    class _FakeRedis:
        def set(self, key, value):
            written[key] = value

    class _FakeRedisModule:
        @staticmethod
        def from_url(url, **kwargs):
            return _FakeRedis()

    monkeypatch.setenv("AGENT_NAME", "harmony")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    with patch.dict(sys.modules, {"redis": _FakeRedisModule()}):
        sc._persist_agent_model_to_redis(
            "anthropic/claude-3-5-sonnet-20241022",
            "openrouter",
            "",
        )

    assert "agent:harmony:settings:model" in written
    payload = json.loads(written["agent:harmony:settings:model"])
    assert payload["model"] == "anthropic/claude-3-5-sonnet-20241022"
    assert payload["provider"] == "openrouter"
    assert payload["base_url"] == ""


def test_persist_agent_model_to_redis_noop_without_env(monkeypatch):
    """_persist_agent_model_to_redis silently no-ops when AGENT_NAME/REDIS_URL absent."""
    import gateway.slash_commands as sc

    call_count = [0]

    class _FakeRedisModule:
        @staticmethod
        def from_url(*a, **kw):
            call_count[0] += 1
            return MagicMock()

    monkeypatch.delenv("AGENT_NAME", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    with patch.dict(sys.modules, {"redis": _FakeRedisModule()}):
        sc._persist_agent_model_to_redis("any/model", "openrouter")

    assert call_count[0] == 0, "Redis should not be contacted when env vars absent"


def test_persist_agent_model_to_redis_swallows_errors(monkeypatch):
    """Errors from Redis are swallowed; no exception propagates."""
    import gateway.slash_commands as sc

    class _BrokenRedis:
        def set(self, key, value):
            raise ConnectionError("Redis down")

    class _FakeRedisModule:
        @staticmethod
        def from_url(*a, **kw):
            return _BrokenRedis()

    monkeypatch.setenv("AGENT_NAME", "harmony")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    with patch.dict(sys.modules, {"redis": _FakeRedisModule()}):
        # Should not raise.
        sc._persist_agent_model_to_redis("any/model", "openrouter")


# ---------------------------------------------------------------------------
# New: apply_agent_settings._apply_model — startup re-apply
# ---------------------------------------------------------------------------

def test_apply_model_writes_model_to_config(tmp_path):
    """_apply_model() patches model.default and model.provider into config.yaml."""
    import yaml
    from skills.hive_mind.apply_agent_settings import _apply_model, HERMES_HOME

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "model:\n  provider: openrouter\n  summary_model: google/gemini-flash\n",
        encoding="utf-8",
    )

    import skills.hive_mind.apply_agent_settings as _m
    original_home = _m.HERMES_HOME
    _m.HERMES_HOME = tmp_path
    try:
        raw = json.dumps({
            "model": "anthropic/claude-3-5-sonnet-20241022",
            "provider": "openrouter",
            "base_url": "",
        })
        _apply_model(raw)
    finally:
        _m.HERMES_HOME = original_home

    cfg = yaml.safe_load(config_yaml.read_text())
    assert cfg["model"]["default"] == "anthropic/claude-3-5-sonnet-20241022"
    assert cfg["model"]["provider"] == "openrouter"
    # summary_model should be preserved.
    assert cfg["model"]["summary_model"] == "google/gemini-flash"


def test_apply_model_handles_flat_string_model_key(tmp_path):
    """_apply_model() handles config.yaml with ``model: <name>`` (flat string)."""
    import yaml
    from skills.hive_mind.apply_agent_settings import _apply_model

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("model: some-old-flat-string\n", encoding="utf-8")

    import skills.hive_mind.apply_agent_settings as _m
    _m.HERMES_HOME = tmp_path
    try:
        _apply_model(json.dumps({
            "model": "openai/gpt-4o",
            "provider": "openrouter",
            "base_url": "",
        }))
    finally:
        pass  # HERMES_HOME already patched in-place

    cfg = yaml.safe_load(config_yaml.read_text())
    assert isinstance(cfg["model"], dict)
    assert cfg["model"]["default"] == "openai/gpt-4o"


def test_apply_model_ignores_malformed_payload(tmp_path, capsys):
    """Malformed JSON payload is skipped with a warning, not an exception."""
    import skills.hive_mind.apply_agent_settings as _m
    from skills.hive_mind.apply_agent_settings import _apply_model

    (tmp_path / "config.yaml").write_text("model:\n  provider: openrouter\n")
    _m.HERMES_HOME = tmp_path

    _apply_model("not-valid-json{{")  # should not raise
    _apply_model(json.dumps({"no_model_key": "oops"}))  # missing 'model' key

    # Config must be unchanged.
    import yaml
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "default" not in cfg.get("model", {})


def test_apply_model_skips_when_already_set(tmp_path, capsys):
    """_apply_model() is a no-op when the config already has the same value."""
    import yaml
    import skills.hive_mind.apply_agent_settings as _m
    from skills.hive_mind.apply_agent_settings import _apply_model

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "model:\n  default: anthropic/claude-3\n  provider: openrouter\n",
        encoding="utf-8",
    )
    mtime_before = config_yaml.stat().st_mtime
    _m.HERMES_HOME = tmp_path

    _apply_model(json.dumps({"model": "anthropic/claude-3", "provider": "openrouter"}))

    # File should not have been rewritten (mtime unchanged).
    assert config_yaml.stat().st_mtime == mtime_before


def test_apply_model_restores_from_redis_in_main(tmp_path, monkeypatch):
    """main() reads agent:{name}:settings:model from Redis and calls _apply_model."""
    import yaml
    import skills.hive_mind.apply_agent_settings as _m

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("model:\n  provider: openrouter\n", encoding="utf-8")
    _m.HERMES_HOME = tmp_path
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    redis_data = {
        "agent:testbot:settings:model": json.dumps({
            "model": "google/gemini-pro-1.5",
            "provider": "openrouter",
            "base_url": "",
        }),
    }

    class _FakeRedis:
        def get(self, key):
            return redis_data.get(key)

    class _FakeRedisModule:
        @staticmethod
        def from_url(*a, **kw):
            return _FakeRedis()

    with patch.dict(sys.modules, {"redis": _FakeRedisModule()}):
        with patch("sys.argv", ["apply_agent_settings.py", "--agent", "testbot"]):
            rc = _m.main()

    assert rc == 0
    cfg = yaml.safe_load(config_yaml.read_text())
    assert cfg["model"]["default"] == "google/gemini-pro-1.5"
