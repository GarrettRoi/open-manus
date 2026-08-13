"""Tests for per-task model routing (hermes_cli/model_routing.py) — #118.

Covers normalization, category fallbacks, and the resolution precedence
contract: explicit pin > routing rule > primary model.
"""

import pytest

from hermes_cli.model_routing import (
    AUX_TASK_CATEGORIES,
    ROUTING_CATEGORIES,
    normalize_routing_config,
    resolve_routed_model,
    resolve_routed_model_for_aux_task,
)


class TestNormalizeRoutingConfig:
    def test_none_and_non_dict(self):
        assert normalize_routing_config(None) == {}
        assert normalize_routing_config("cron_job: x") == {}
        assert normalize_routing_config(42) == {}

    def test_string_rule(self):
        out = normalize_routing_config({"cron_job": "deepseek/deepseek-chat"})
        assert out == {"cron_job": {"model": "deepseek/deepseek-chat", "provider": ""}}

    def test_dict_rule_with_provider(self):
        out = normalize_routing_config(
            {"code": {"model": "minimax/minimax-m2.5", "provider": "openrouter"}}
        )
        assert out["code"] == {"model": "minimax/minimax-m2.5", "provider": "openrouter"}

    def test_invalid_rules_dropped(self):
        out = normalize_routing_config({
            "cron_job": "",            # empty
            "chat": "auto",            # sentinel — not a real model
            "code": {"provider": "x"},  # no model
            "quick_response": 3,        # wrong type
            "background_task": "google/gemini-2.5-flash-preview",
        })
        assert list(out) == ["background_task"]

    def test_unknown_category_kept(self):
        out = normalize_routing_config({"my_custom": "some/model"})
        assert out["my_custom"]["model"] == "some/model"


class TestResolveRoutedModel:
    def test_no_routing_section(self):
        assert resolve_routed_model({}, "cron_job") is None
        assert resolve_routed_model(None, "cron_job") is None
        assert resolve_routed_model({"routing": {}}, "cron_job") is None

    def test_direct_hit(self):
        cfg = {"routing": {"cron_job": "deepseek/deepseek-chat"}}
        assert resolve_routed_model(cfg, "cron_job")["model"] == "deepseek/deepseek-chat"

    def test_cron_falls_back_to_background_task(self):
        cfg = {"routing": {"background_task": "google/gemini-2.5-flash-preview"}}
        routed = resolve_routed_model(cfg, "cron_job")
        assert routed["model"] == "google/gemini-2.5-flash-preview"

    def test_delegation_falls_back_to_background_task(self):
        cfg = {"routing": {"background_task": "cheap/model"}}
        assert resolve_routed_model(cfg, "delegation")["model"] == "cheap/model"

    def test_specific_rule_beats_fallback(self):
        cfg = {"routing": {
            "cron_job": "deepseek/deepseek-chat",
            "background_task": "google/gemini-2.5-flash-preview",
        }}
        assert resolve_routed_model(cfg, "cron_job")["model"] == "deepseek/deepseek-chat"

    def test_unrelated_category_no_bleed(self):
        # chat/code do not fall back to background_task.
        cfg = {"routing": {"background_task": "cheap/model"}}
        assert resolve_routed_model(cfg, "chat") is None
        assert resolve_routed_model(cfg, "code") is None

    def test_returns_copy(self):
        cfg = {"routing": {"cron_job": "a/b"}}
        r1 = resolve_routed_model(cfg, "cron_job")
        r1["model"] = "mutated"
        assert resolve_routed_model(cfg, "cron_job")["model"] == "a/b"


class TestAuxTaskRouting:
    def test_vision_maps_to_multimodal(self):
        cfg = {"routing": {
            "multimodal": "google/gemini-2.5-pro-preview",
            "background_task": "cheap/model",
        }}
        assert (
            resolve_routed_model_for_aux_task(cfg, "vision")["model"]
            == "google/gemini-2.5-pro-preview"
        )

    def test_other_aux_tasks_use_background_task(self):
        cfg = {"routing": {"background_task": "cheap/model"}}
        assert resolve_routed_model_for_aux_task(cfg, "compression")["model"] == "cheap/model"
        assert resolve_routed_model_for_aux_task(cfg, "web_extract")["model"] == "cheap/model"

    def test_no_rule_returns_none(self):
        assert resolve_routed_model_for_aux_task({}, "vision") is None


class TestPrecedenceContract:
    """Explicit pin > routing rule > primary model."""

    CFG = {
        "model": "moonshotai/kimi-k2.5",
        "routing": {"cron_job": "deepseek/deepseek-chat"},
    }

    @staticmethod
    def _cron_effective(job_model: str, cfg: dict) -> str:
        """Mirror cron/scheduler.py resolution: pin > routing > primary."""
        if (job_model or "").strip():
            return job_model
        routed = resolve_routed_model(cfg, "cron_job")
        if routed and routed.get("model"):
            return routed["model"]
        return cfg.get("model", "")

    def test_pin_wins(self):
        assert self._cron_effective("openai/gpt-5.2", self.CFG) == "openai/gpt-5.2"

    def test_routing_beats_primary(self):
        assert self._cron_effective("", self.CFG) == "deepseek/deepseek-chat"

    def test_primary_fallback(self):
        cfg = {"model": "moonshotai/kimi-k2.5", "routing": {}}
        assert self._cron_effective("", cfg) == "moonshotai/kimi-k2.5"


class TestCatalog:
    def test_categories_present(self):
        for cat in ("cron_job", "background_task", "quick_response", "code",
                    "chat", "delegation", "multimodal"):
            assert cat in ROUTING_CATEGORIES

    def test_aux_map_only_known_categories(self):
        for cat in AUX_TASK_CATEGORIES.values():
            assert cat in ROUTING_CATEGORIES


class TestDefaultConfigSection:
    def test_default_config_has_routing(self):
        from hermes_cli.config import DEFAULT_CONFIG
        routing = DEFAULT_CONFIG.get("routing")
        assert isinstance(routing, dict)
        for key in ("cron_job", "background_task", "quick_response", "code", "chat"):
            assert routing[key] == ""


class TestDelegationRouting:
    def test_delegation_credentials_pick_routed_model(self, monkeypatch):
        import tools.delegate_tool as dt

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"routing": {"delegation": "cheap/model"}},
        )
        creds = dt._resolve_delegation_credentials({}, parent_agent=None)
        assert creds["model"] == "cheap/model"
        assert creds["provider"] is None  # inherits parent credentials

    def test_explicit_delegation_model_wins(self, monkeypatch):
        import tools.delegate_tool as dt

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"routing": {"delegation": "cheap/model"}},
        )
        creds = dt._resolve_delegation_credentials(
            {"model": "openai/gpt-5.2"}, parent_agent=None
        )
        assert creds["model"] == "openai/gpt-5.2"
