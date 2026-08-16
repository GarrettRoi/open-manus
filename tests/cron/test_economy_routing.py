"""Economy-tier downshift + escalation tests (task: agent-driven cheap-LLM routing).

Covers:
  * downshift persistence (previous model remembered, survives reload)
  * owner/auto revert (escalate back to the strong model)
  * rolling-outcome bookkeeping and the auto-pin threshold
  * scheduler retry-on-primary for a failed downshifted run (run_job mocked)
  * economy model resolution from routing config
"""
import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json doesn't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _make_job(model=None):
    from cron.jobs import create_job
    kwargs = {}
    if model:
        kwargs["model"] = model
    return create_job(prompt="summarize the feed", schedule="every 30m",
                      name="feed-summary", **kwargs)


# ---------------------------------------------------------------------------
# Downshift / revert persistence
# ---------------------------------------------------------------------------

class TestDownshiftPersistence:
    def test_downshift_pins_economy_and_remembers_previous(self, temp_home):
        from cron.economy import apply_downshift, economy_state, is_downshifted
        from cron.jobs import get_job

        job = _make_job(model="anthropic/claude-opus-4.6")
        res = apply_downshift(job["id"], model="deepseek/deepseek-chat",
                              provider="openrouter", reason="owner approved")
        assert res["old_model"] == "anthropic/claude-opus-4.6"
        assert res["new_model"] == "deepseek/deepseek-chat"

        stored = get_job(job["id"])
        assert stored["model"] == "deepseek/deepseek-chat"
        assert is_downshifted(stored)
        state = economy_state(stored)
        assert state["previous_model"] == "anthropic/claude-opus-4.6"
        assert state["reason"] == "owner approved"

    def test_downshift_unpinned_job_remembers_empty_previous(self, temp_home):
        from cron.economy import apply_downshift, economy_state
        from cron.jobs import get_job

        job = _make_job()  # unpinned — follows config/routing
        apply_downshift(job["id"], model="deepseek/deepseek-chat", provider=None)
        state = economy_state(get_job(job["id"]))
        assert state["previous_model"] == ""

    def test_double_downshift_keeps_original_previous(self, temp_home):
        from cron.economy import apply_downshift, economy_state
        from cron.jobs import get_job

        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap-a", provider=None)
        apply_downshift(job["id"], model="cheap-b", provider=None)
        state = economy_state(get_job(job["id"]))
        # escalation must return to the true strong model, never cheap-a
        assert state["previous_model"] == "strong-model"

    def test_downshift_missing_job_returns_none(self, temp_home):
        from cron.economy import apply_downshift
        assert apply_downshift("nope", model="cheap", provider=None) is None

    def test_revert_restores_previous_model(self, temp_home):
        from cron.economy import apply_downshift, revert_downshift, is_downshifted
        from cron.jobs import get_job

        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap", provider=None)
        res = revert_downshift(job["id"], reason="owner unhappy", source="owner")
        assert res["new_model"] == "strong-model"
        stored = get_job(job["id"])
        assert stored["model"] == "strong-model"
        assert not is_downshifted(stored)
        assert stored["economy_routing"]["reverted_by"] == "owner"

    def test_revert_unpinned_job_pins_primary_model(self, temp_home):
        """Reverting a job that was unpinned before the downshift must PIN the
        primary model — leaving it unpinned would re-resolve the cheap
        routing.cron_job rule and keep it on the economy tier."""
        from cron.economy import apply_downshift, revert_downshift
        from cron.jobs import get_job

        job = _make_job()
        apply_downshift(job["id"], model="cheap", provider=None)
        revert_downshift(job["id"], source="auto")
        # conftest pins HERMES_MODEL=test-cron-default-model as the primary
        assert get_job(job["id"])["model"] == "test-cron-default-model"

    def test_downshift_clears_foreign_provider_pin(self, temp_home):
        """A bare-string economy rule must not send the economy model to the
        job's previously pinned provider — the pin is cleared so the default
        provider resolves."""
        from cron.economy import apply_downshift
        from cron.jobs import create_job, get_job

        job = create_job(prompt="x", schedule="every 30m", name="p",
                         model="strong", provider="anthropic")
        apply_downshift(job["id"], model="cheap", provider=None)
        assert get_job(job["id"]).get("provider") is None

    def test_revert_restores_previous_provider_pin(self, temp_home):
        from cron.economy import apply_downshift, revert_downshift
        from cron.jobs import create_job, get_job

        job = create_job(prompt="x", schedule="every 30m", name="p",
                         model="strong", provider="anthropic")
        apply_downshift(job["id"], model="cheap", provider="openrouter")
        revert_downshift(job["id"], source="owner")
        stored = get_job(job["id"])
        assert stored["model"] == "strong"
        assert stored["provider"] == "anthropic"

    def test_revert_clears_economy_provider_when_previously_unpinned_provider(self, temp_home):
        from cron.economy import apply_downshift, revert_downshift
        from cron.jobs import get_job

        job = _make_job(model="strong")  # no provider pin
        apply_downshift(job["id"], model="cheap", provider="openrouter")
        revert_downshift(job["id"], source="owner")
        stored = get_job(job["id"])
        assert stored["model"] == "strong"
        assert stored.get("provider") is None

    def test_escalation_target_for_unpinned_job_is_primary_not_routing(self, temp_home, monkeypatch):
        """Regression: with routing.cron_job == routing.economy (fleet default),
        an unpinned downshifted job must escalate to the PRIMARY model, never
        re-resolve the cheap routing rule."""
        from cron import economy
        from cron.economy import apply_downshift, escalation_target
        from cron.jobs import get_job

        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.setattr(
            economy, "_resolve_primary",
            lambda: {"model": "anthropic/claude-opus-4.6", "provider": "openrouter"},
        )
        job = _make_job()  # unpinned → routing.cron_job would apply
        apply_downshift(job["id"], model="deepseek/deepseek-chat", provider=None)
        provider, model = escalation_target(get_job(job["id"]))
        assert model == "anthropic/claude-opus-4.6"
        assert provider == "openrouter"

    def test_auto_pin_of_unpinned_job_pins_primary(self, temp_home, monkeypatch):
        """Auto-pin after repeated escalations must leave the job PINNED to the
        primary model so the owner notice ('will stay on the stronger model')
        is true even when routing.cron_job == routing.economy."""
        from cron import economy
        from cron.economy import apply_downshift, revert_downshift
        from cron.jobs import get_job

        monkeypatch.setattr(
            economy, "_resolve_primary",
            lambda: {"model": "primary-strong", "provider": ""},
        )
        job = _make_job()
        apply_downshift(job["id"], model="deepseek/deepseek-chat", provider=None)
        res = revert_downshift(job["id"], source="auto")
        assert res["new_model"] == "primary-strong"
        assert get_job(job["id"])["model"] == "primary-strong"

    def test_revert_not_downshifted_returns_none(self, temp_home):
        from cron.economy import revert_downshift
        job = _make_job(model="strong")
        assert revert_downshift(job["id"]) is None


# ---------------------------------------------------------------------------
# Outcome bookkeeping + auto-pin threshold
# ---------------------------------------------------------------------------

class TestAutoPinThreshold:
    def _downshifted_job(self):
        from cron.economy import apply_downshift
        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap", provider=None)
        return job

    def test_single_escalation_does_not_pin(self, temp_home):
        from cron.economy import record_run_outcome
        job = self._downshifted_job()
        assert record_run_outcome(job["id"], escalated=True, success=True) is False

    def test_two_escalations_in_window_trigger_pin(self, temp_home):
        from cron.economy import record_run_outcome
        job = self._downshifted_job()
        record_run_outcome(job["id"], escalated=True, success=True)
        record_run_outcome(job["id"], escalated=False, success=True)
        assert record_run_outcome(job["id"], escalated=True, success=False) is True

    def test_old_escalations_age_out_of_window(self, temp_home):
        from cron.economy import record_run_outcome
        job = self._downshifted_job()
        record_run_outcome(job["id"], escalated=True, success=True)
        for _ in range(5):  # push the escalation out of the last-5 window
            record_run_outcome(job["id"], escalated=False, success=True)
        assert record_run_outcome(job["id"], escalated=True, success=True) is False

    def test_outcomes_persist_on_job_record(self, temp_home):
        from cron.economy import record_run_outcome, economy_state
        from cron.jobs import get_job
        job = self._downshifted_job()
        record_run_outcome(job["id"], escalated=True, success=False)
        state = economy_state(get_job(job["id"]))
        assert state["escalation_count"] == 1
        assert state["recent"][-1]["escalated"] is True
        assert state["recent"][-1]["success"] is False

    def test_record_on_non_downshifted_job_is_noop(self, temp_home):
        from cron.economy import record_run_outcome
        job = _make_job(model="strong")
        assert record_run_outcome(job["id"], escalated=True, success=False) is False


# ---------------------------------------------------------------------------
# Scheduler escalation retry (run_job mocked)
# ---------------------------------------------------------------------------

class TestSchedulerEscalationRetry:
    def _run(self, monkeypatch, job, results):
        """Run run_one_job with run_job returning queued results; captures calls."""
        import cron.scheduler as sched
        calls = []

        def fake_run_job(j, **kw):
            calls.append(dict(j))
            return results[min(len(calls) - 1, len(results) - 1)]

        monkeypatch.setattr(sched, "run_job", fake_run_job)
        monkeypatch.setattr(sched, "_deliver_result", lambda *a, **k: None)
        monkeypatch.setattr(sched, "_log_cron_run_to_discord", lambda *a, **k: None)
        assert sched.run_one_job(job) is True
        return calls

    def test_failed_economy_run_retries_on_previous_model(self, temp_home, monkeypatch):
        from cron.economy import apply_downshift
        from cron.jobs import get_job

        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap", provider=None)
        job = get_job(job["id"])

        calls = self._run(monkeypatch, job, [
            (False, "doc", "", "provider exploded"),
            (True, "doc", "all good", None),
        ])
        assert len(calls) == 2
        assert calls[0]["model"] == "cheap"
        assert calls[1]["model"] == "strong-model"
        # provider restored to the pre-downshift pin (None = unpinned)
        assert calls[1].get("provider") is None
        # escalated run recorded on the job
        state = get_job(job["id"])["economy_routing"]
        assert state["escalation_count"] == 1

    def test_empty_response_counts_as_failure_and_escalates(self, temp_home, monkeypatch):
        from cron.economy import apply_downshift
        from cron.jobs import get_job

        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap", provider=None)
        calls = self._run(monkeypatch, get_job(job["id"]), [
            (True, "doc", "   ", None),  # "success" but empty → escalate
            (True, "doc", "real output", None),
        ])
        assert len(calls) == 2

    def test_successful_economy_run_does_not_retry(self, temp_home, monkeypatch):
        from cron.economy import apply_downshift
        from cron.jobs import get_job

        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap", provider=None)
        calls = self._run(monkeypatch, get_job(job["id"]), [
            (True, "doc", "fine", None),
        ])
        assert len(calls) == 1

    def test_failed_normal_run_does_not_retry(self, temp_home, monkeypatch):
        job = _make_job(model="strong-model")
        calls = self._run(monkeypatch, job, [
            (False, "doc", "", "boom"),
        ])
        assert len(calls) == 1

    def test_repeated_escalations_auto_pin_and_notify(self, temp_home, monkeypatch):
        import cron.scheduler as sched
        from cron.economy import apply_downshift, is_downshifted
        from cron.jobs import get_job

        job = _make_job(model="strong-model")
        apply_downshift(job["id"], model="cheap", provider=None)

        delivered = []
        monkeypatch.setattr(
            sched, "_deliver_result",
            lambda j, content, **kw: delivered.append(content) or None,
        )
        monkeypatch.setattr(sched, "_log_cron_run_to_discord", lambda *a, **k: None)
        monkeypatch.setattr(
            sched, "run_job",
            lambda j, **kw: (False, "doc", "", "always fails")
            if j.get("model") == "cheap" else (True, "doc", "ok", None),
        )

        sched.run_one_job(get_job(job["id"]))  # escalation 1 — no pin yet
        assert is_downshifted(get_job(job["id"]))
        sched.run_one_job(get_job(job["id"]))  # escalation 2 — auto-pin

        stored = get_job(job["id"])
        assert not is_downshifted(stored)
        assert stored["model"] == "strong-model"
        assert any("moved back" in d for d in delivered)


# ---------------------------------------------------------------------------
# Economy model resolution
# ---------------------------------------------------------------------------

class TestEconomyModelResolution:
    def test_explicit_economy_rule(self):
        from hermes_cli.model_routing import resolve_economy_model
        cfg = {"routing": {"economy": "deepseek/deepseek-chat"}}
        assert resolve_economy_model(cfg)["model"] == "deepseek/deepseek-chat"

    def test_falls_back_to_cron_job_then_background(self):
        from hermes_cli.model_routing import resolve_economy_model
        assert resolve_economy_model(
            {"routing": {"cron_job": "cheap-cron"}})["model"] == "cheap-cron"
        assert resolve_economy_model(
            {"routing": {"background_task": "cheap-bg"}})["model"] == "cheap-bg"

    def test_no_routing_returns_none(self):
        from hermes_cli.model_routing import resolve_economy_model
        assert resolve_economy_model({}) is None
        assert resolve_economy_model(None) is None
