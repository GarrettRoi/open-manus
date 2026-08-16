b.get("id", "?"), e)

    # Wake-gate: if this job has a pre-check script, run it BEFORE building
    # the prompt so a ``{"wakeAgent": false}`` response can short-circuit
    # the whole agent run. We pass the result into _build_job_prompt so
    # the script is only executed once.
    prerun_script = None
    script_path = job.get("script")
    if script_path:
        prerun_script = _run_job_script(script_path)
        _ran_ok, _script_output = prerun_script
        if _ran_ok and not _parse_wake_gate(_script_output):
            logger.info(
                "Job '%s' (ID: %s): wakeAgent=false, skipping agent run",
                job_name, job_id,
            )
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "Script gate returned `wakeAgent=false` — agent skipped.\n"
            )
            return True, silent_doc, SILENT_MARKER, None

    try:
        prompt = _build_job_prompt(job, prerun_script=prerun_script)
    except CronPromptInjectionBlocked as block_exc:
        # Assembled prompt (user prompt + loaded skill content) tripped the
        # injection scanner. Refuse to run the agent this tick and surface
        # a clear failure to the operator so they see WHY the scheduled job
        # didn't run and can audit the offending skill.
        logger.warning(
            "Job '%s' (ID: %s): blocked by prompt-injection scanner — %s",
            job_name, job_id, block_exc,
        )
        blocked_doc = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Status:** BLOCKED\n\n"
            "The assembled prompt (user prompt + loaded skill content) tripped "
            "the cron injection scanner and the agent was NOT run.\n\n"
            f"**Scanner result:** {block_exc}\n\n"
            "Audit the skill(s) attached to this job for prompt-injection "
            "payloads or invisible-unicode markers. If the skill is legitimate "
            "and the match is a false positive, rephrase the content to avoid "
            "the threat pattern (`tools/cronjob_tools.py::_CRON_THREAT_PATTERNS`)."
        )
        return False, blocked_doc, "", str(block_exc)
    if prompt is None:
        logger.info("Job '%s': script produced no output, skipping AI call.", job_name)
        return True, "", SILENT_MARKER, None
    origin = _resolve_origin(job)
    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"

    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info("Prompt: %s", prompt[:100])

    agent = None

    # Mark this as a cron session so the approval system can apply cron_mode.
    # This env var is process-wide and persists for the lifetime of the
    # scheduler process — every job this process runs is a cron job.
    os.environ["HERMES_CRON_SESSION"] = "1"

    # Use ContextVars for per-job session/delivery state so parallel jobs
    # don't clobber each other's targets (os.environ is process-global).
    from gateway.session_context import set_session_vars, clear_session_vars, _VAR_MAP

    # Cron execution is an internal scheduler context, not a live inbound
    # gateway message. Do not seed HERMES_SESSION_* contextvars from the
    # stored ``origin`` (which is delivery routing metadata, not a sender
    # identity). Several tool consumers branch on these vars during job
    # execution and would otherwise behave as if a real user from the
    # origin chat was driving the agent:
    #   - tools/terminal_tool.py: background-process notification routing
    #     (notify_on_complete / watch_patterns) reads HERMES_SESSION_PLATFORM
    #     and HERMES_SESSION_CHAT_ID to populate watcher_platform / chat_id,
    #     which would route completion notifications to the origin chat
    #     instead of via HERMES_CRON_AUTO_DELIVER_* below.
    #   - tools/tts_tool.py: picks Opus vs MP3 based on
    #     HERMES_SESSION_PLATFORM == "telegram".
    #   - tools/skills_tool.py + agent/prompt_builder.py: per-platform
    #     skill-disable lists and the system-prompt cache key both consume
    #     HERMES_SESSION_PLATFORM.
    #   - tools/send_message_tool.py: mirror source labelling and the
    #     send_message gate read HERMES_SESSION_PLATFORM.
    # Cron output delivery itself reads job["origin"] directly via
    # _resolve_origin(job) and the HERMES_CRON_AUTO_DELIVER_* vars set
    # below, so clearing HERMES_SESSION_* here does not affect delivery.
    _ctx_tokens = set_session_vars(
        platform="",
        chat_id="",
        chat_name="",
    )
    _cron_delivery_vars = (
        "HERMES_CRON_AUTO_DELIVER_PLATFORM",
        "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
        "HERMES_CRON_AUTO_DELIVER_THREAD_ID",
    )
    for _var_name in _cron_delivery_vars:
        _VAR_MAP[_var_name].set("")

    # Per-job working directory.  When set (and validated at create/update
    # time), we point TERMINAL_CWD at it so:
    #   - build_context_files_prompt() picks up AGENTS.md / CLAUDE.md /
    #     .cursorrules from the job's project dir, AND
    #   - the terminal, file, and code-exec tools run commands from there.
    #
    # os.environ["TERMINAL_CWD"] is process-global, so this override is
    # serialized by _terminal_cwd_lock (acquired just below): a workdir job
    # holds it as a writer for its whole run, excluding every other job, while
    # workdir-less jobs hold it as readers and stay parallel with each other.
    # The sequential pool only keeps workdir jobs from overlapping EACH OTHER;
    # the lock is what additionally keeps a concurrently-firing workdir-less
    # parallel-pool job from observing this override and running its shell /
    # file / code-exec commands in the wrong directory.  For workdir-less jobs
    # we leave TERMINAL_CWD untouched — preserves the original behaviour
    # (skip_context_files=True, tools use whatever cwd the scheduler has).
    _job_workdir = (job.get("workdir") or "").strip() or None
    if _job_workdir and not Path(_job_workdir).is_dir():
        # Directory was removed between create-time validation and now.  Log
        # and drop back to old behaviour rather than crashing the job.
        logger.warning(
            "Job '%s': configured workdir %r no longer exists — running without it",
            job_id, _job_workdir,
        )
        _job_workdir = None

    # Snapshot the current env value BEFORE acquiring the lock so the finally
    # below can always restore it, even if an exception fires before we set the
    # override inside the try.  This read can't leak the lock (it precedes the
    # acquire) and is a no-op for workdir-less jobs (they never mutate the env).
    _prior_terminal_cwd = os.environ.get("TERMINAL_CWD", "_UNSET_")

    _holds_cwd_write = _job_workdir is not None
    if _holds_cwd_write:
        _terminal_cwd_lock.acquire_write()
    else:
        _terminal_cwd_lock.acquire_read()

    # Everything after the acquire MUST live inside this try, so the finally
    # below always releases the lock even if the env override or any later
    # statement raises.  A leaked writer would deadlock the whole scheduler
    # (every future job blocks on acquire_*); a leaked reader blocks all
    # future writers.  Acquire itself can't leak (it either blocks or returns).
    try:
        if _job_workdir:
            os.environ["TERMINAL_CWD"] = _job_workdir
            logger.info("Job '%s': using workdir %s", job_id, _job_workdir)

        # Re-read .env and config.yaml fresh every run so provider/key
        # changes take effect without a gateway restart. Route through
        # load_hermes_dotenv (not a bare load_dotenv) and reset the secret-
        # source cache first: startup already applied external secrets and
        # recorded this HERMES_HOME in _APPLIED_HOMES, so a naive reload would
        # re-apply only the .env placeholder and never re-resolve a Bitwarden/
        # BSM-backed secret — leaving cron jobs 401'ing on the placeholder
        # (#33465). Clearing the cache forces the re-pull; the resolved secret
        # overrides the placeholder only when secrets.bitwarden.override_existing
        # is set (mirrors startup), and the Bitwarden value-cache keeps the
        # forced re-pull off the network. load_hermes_dotenv also handles the
        # utf-8/latin-1 encoding fallback internally.
        from hermes_cli.env_loader import (
            load_hermes_dotenv,
            reset_secret_source_cache,
        )
        reset_secret_source_cache()
        load_hermes_dotenv(hermes_home=_get_hermes_home())

        delivery_target = _resolve_delivery_target(job)
        if delivery_target:
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set(delivery_target["platform"])
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set(str(delivery_target["chat_id"]))
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set(
                ""
                if delivery_target.get("thread_id") is None
                else str(delivery_target["thread_id"])
            )

        # Model resolution precedence: per-job override > HERMES_MODEL env >
        # config.yaml ``model:`` (string or ``{default: ...}``). The per-job
        # value is intentionally re-read from storage every tick so a
        # ``cronjob action=update model=...`` after a failed run takes effect
        # on the next tick — there is no in-memory cache.
        model = job.get("model") or os.getenv("HERMES_MODEL") or ""

        # Load config.yaml for model, reasoning, prefill, toolsets, provider routing
        _cfg = {}
        try:
            import yaml
            _cfg_path = str(_get_hermes_home() / "config.yaml")
            if os.path.exists(_cfg_path):
                with open(_cfg_path, encoding="utf-8") as _f:
                    _cfg = yaml.safe_load(_f) or {}
                # Managed scope: a scheduled job must honor administrator-pinned
                # model / reasoning / toolsets / provider_routing too. This loader
                # builds its own dict, so overlay managed values via the shared
                # helper (fail-open, no-op when no managed scope).
                try:
                    from hermes_cli import managed_scope
                    _cfg = managed_scope.apply_managed_overlay(_cfg)
                except Exception:
                    pass
                _cfg = _expand_env_vars(_cfg)
                # Coerce null/missing to {} so a falsy default never
                # clobbers an already-resolved env value with ``None``.
                _model_cfg = _cfg.get("model") or {}
                if not job.get("model"):
                    if isinstance(_model_cfg, str):
                        model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        # Mirror the CLI/oneshot resolution: prefer ``default``,
                        # accept a ``model`` alias, overwrite only when truthy.
                        _default = _model_cfg.get("default") or _model_cfg.get("model")
                        if _default:
                            model = _default
        except Exception as e:
            logger.warning("Job '%s': failed to load config.yaml, using defaults: %s", job_id, e)

        # Fail fast if no model resolved from job / env / config.yaml: an empty
        # model otherwise reaches the provider as an opaque 400 (#23979).
        if not (isinstance(model, str) and model.strip()):
            raise RuntimeError(
                f"Cron job '{job_name}' has no model configured "
                f"(job.model={job.get('model')!r}, "
                f"HERMES_MODEL={os.getenv('HERMES_MODEL', '')!r}, "
                "config.yaml model.default missing or empty). "
                f"Set a per-job model via "
                f"`cronjob action=update job_id={job_id} model=<name>` or set a "
                "default with `hermes model <name>`."
            )

        # Apply IPv4 preference if configured.
        try:
            from hermes_constants import apply_ipv4_preference
            _net_cfg = _cfg.get("network", {})
            if isinstance(_net_cfg, dict) and _net_cfg.get("force_ipv4"):
                apply_ipv4_preference(force=True)
        except Exception:
            pass

        # Reasoning config from config.yaml (raw value — a YAML boolean False
        # means thinking disabled, see parse_reasoning_effort)
        from hermes_constants import parse_reasoning_effort
        reasoning_config = parse_reasoning_effort(
            _cfg.get("agent", {}).get("reasoning_effort", "")
        )

        # Prefill messages from env or config.yaml. The top-level
        # prefill_messages_file key is canonical; agent.prefill_messages_file is
        # retained as a legacy fallback for older CLI/godmode configs.
        prefill_messages = None
        agent_cfg = _cfg.get("agent", {}) if isinstance(_cfg.get("agent", {}), dict) else {}
        prefill_file = (
            os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
            or _cfg.get("prefill_messages_file", "")
            or agent_cfg.get("prefill_messages_file", "")
        )
        if prefill_file:
            pfpath = Path(prefill_file).expanduser()
            if not pfpath.is_absolute():
                pfpath = _get_hermes_home() / pfpath
            if pfpath.exists():
                try:
                    with open(pfpath, "r", encoding="utf-8") as _pf:
                        prefill_messages = json.load(_pf)
                    if not isinstance(prefill_messages, list):
                        prefill_messages = None
                except Exception as e:
                    logger.warning("Job '%s': failed to parse prefill messages file '%s': %s", job_id, pfpath, e)
                    prefill_messages = None

        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90

        # Provider routing
        pr = _cfg.get("provider_routing") or {}

        from hermes_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )
        from hermes_cli.auth import AuthError

        # F8 runtime backstop: never resolve a stored provider/base_url pair that
        # would ship a named provider's stored credential to an off-host endpoint
        # (CWE-200/CWE-522). The cron tool validates this on create/update, but a
        # job persisted before that guard — or written directly to the jobs store
        # — reaches this sink unchecked. Fail closed before resolution so no
        # off-host call is ever made with a stored key.
        _guard_job_credential_exfil(job)

        try:
            # Do not inject HERMES_INFERENCE_PROVIDER here. resolve_runtime_provider()
            # already prefers persisted config over stale shell/env overrides when
            # no explicit provider is requested. Passing the env var here short-
            # circuits that precedence and can resurrect old providers (for
            # example DeepSeek) for cron jobs that do not pin provider/model.
            runtime_kwargs = {
                "requested": job.get("provider"),
            }
            if job.get("base_url"):
                runtime_kwargs["explicit_base_url"] = job.get("base_url")
            runtime = resolve_runtime_provider(**runtime_kwargs)
        except AuthError as auth_exc:
            # Primary provider auth failed — try fallback chain before giving up.
            logger.warning("Job '%s': primary auth failed (%s), trying fallback", job_id, auth_exc)
            fb_list = get_fallback_chain(_cfg)
            runtime = None
            for entry in fb_list:
                try:
                    fb_kwargs = {"requested": entry.get("provider")}
                    if entry.get("base_url"):
                        fb_kwargs["explicit_base_url"] = entry["base_url"]
                    if entry.get("api_key"):
                        fb_kwargs["explicit_api_key"] = entry["api_key"]
                    runtime = resolve_runtime_provider(**fb_kwargs)
                    logger.info("Job '%s': fallback resolved to %s", job_id, runtime.get("provider"))
                    break
                except Exception as fb_exc:
                    logger.debug("Job '%s': fallback %s failed: %s", job_id, entry.get("provider"), fb_exc)
            if runtime is None:
                raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc

        # Provider/model-drift fail-closed guard (#44585).
        #
        # An UNPINNED job (no explicit job["provider"]/["model"]) follows the
        # global default, which can change after the job was created — a switch
        # to a paid PROVIDER (e.g. nous) OR a paid MODEL on the same provider
        # (e.g. claude-fable-5 on openrouter). Without a guard the job would
        # silently inherit that change and spend real money on every tick — the
        # $7.73 incident named BOTH a provider and a model.
        #
        # create_job() snapshots whatever resolution would have picked at
        # creation for each unpinned axis (job["provider_snapshot"] /
        # job["model_snapshot"]). Here, for each axis that (a) has a snapshot and
        # (b) is unpinned and (c) currently resolves to a DIFFERENT value, we
        # fail closed: skip this run, make NO paid call, and deliver a loud,
        # actionable alert telling the user to pin the axis explicitly.
        #
        # Back-compat: an axis with no snapshot (pre-existing jobs, no_agent, or
        # any axis whose creation-time resolution failed) behaves exactly as
        # before — the guard never engages for it. Pinned axes are unaffected.
        _drift: list[str] = []
        _provider_snapshot = (job.get("provider_snapshot") or "").strip().lower()
        if _provider_snapshot and not (job.get("provider") or "").strip():
            _current_provider = str(runtime.get("provider") or "").strip().lower()
            if _current_provider and _current_provider != _provider_snapshot:
                _drift.append(
                    f"provider '{_provider_snapshot}' -> '{_current_provider}'"
                )
        _model_snapshot = (job.get("model_snapshot") or "").strip().lower()
        if _model_snapshot and not (job.get("model") or "").strip():
            _current_model = str(model or "").strip().lower()
            if _current_model and _current_model != _model_snapshot:
                _drift.append(
                    f"model '{_model_snapshot}' -> '{_current_model}'"
                )
        if _drift:
            _changes = "; ".join(_drift)
            logger.warning(
                "Job '%s': SKIPPED — global inference config drifted since "
                "creation (%s) and this job is unpinned. Skipped to prevent "
                "unintended spend. Pin explicitly to proceed: "
                "`cronjob action=update job_id=%s provider=<p> model=<m>`.",
                job_id,
                _changes,
                job_id,
            )
            raise RuntimeError(
                f"Skipped to prevent unintended spend: global inference config "
                f"drifted since this job was created ({_changes}), and this job "
                f"is unpinned. No inference call was made. To run on the new "
                f"config, pin it explicitly: `cronjob action=update "
                f"job_id={job_id} provider=<provider> model=<model>` "
                f"(or pin the original values to keep them). See #44585."
            )

        # Per-task model routing (#118): an UNPINNED job resolves through the
        # agent's ``routing`` rules (cron_job → background_task fallback) so
        # background/polling jobs run on a cheap model instead of the primary.
        # Precedence: per-job pin > routing rule > primary model. Applied
        # AFTER the drift guard so the guard keeps comparing the same
        # primary-model resolution it snapshotted at creation — routing is an
        # explicit owner-configured rule, not silent global drift.
        if not (job.get("model") or "").strip():
            try:
                from hermes_cli.model_routing import resolve_routed_model
                _routed = resolve_routed_model(_cfg, "cron_job")
            except Exception as _route_exc:
                _routed = None
                logger.warning("Job '%s': routing resolution failed: %s", job_id, _route_exc)
            if _routed and _routed.get("model"):
                logger.info(
                    "Job '%s': routing rule applied — model %s -> %s",
                    job_id, model, _routed["model"],
                )
                model = _routed["model"]
                _routed_provider = (_routed.get("provider") or "").strip()
                if _routed_provider and not (job.get("provider") or "").strip():
                    try:
                        runtime = resolve_runtime_provider(requested=_routed_provider)
                    except Exception as _rp_exc:
                        logger.warning(
                            "Job '%s': routed provider '%s' failed to resolve (%s); "
                            "keeping default provider",
                            job_id, _routed_provider, _rp_exc,
                        )

        fallback_model = get_fallback_chain(_cfg) or None
        credential_pool = None
        runtime_provider = str(runtime.get("provider") or "").strip().lower()
        if runtime_provider:
            try:
                from agent.credential_pool import load_pool
                pool = load_pool(runtime_provider)
                if pool.has_credentials():
                    credential_pool = pool
                    logger.info(
                        "Job '%s': loaded credential pool for provider %s with %d entries",
                        job_id,
                        runtime_provider,
                        len(pool.entries()),
                    )
            except Exception as e:
                logger.debug("Job '%s': failed to load credential pool for %s: %s", job_id, runtime_provider, e)

        # Initialize MCP servers so configured mcp_servers are available to
        # the agent's tool registry before AIAgent is constructed. Without
        # this, cron jobs never saw any MCP tools — only the gateway / CLI
        # paths called discover_mcp_tools() at startup. Idempotent: subsequent
        # ticks short-circuit on already-connected servers inside
        # register_mcp_servers(). Non-fatal on failure: a broken MCP server
        # shouldn't kill an otherwise-working cron job. See #4219.
        try:
            from tools.mcp_tool import discover_mcp_tools
            _mcp_tools = discover_mcp_tools()
            if _mcp_tools:
                logger.info(
                    "Job '%s': %d MCP tool(s) available",
                    job_id, len(_mcp_tools),
                )
        except Exception as _mcp_exc:
            logger.warning(
                "Job '%s': MCP initialization failed (non-fatal): %s",
                job_id, _mcp_exc,
            )

        agent = AIAgent(
            model=model,
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            acp_command=runtime.get("command"),
            acp_args=runtime.get("args"),
            max_iterations=max_iterations,
            reasoning_config=reasoning_config,
            prefill_messages=prefill_messages,
            fallback_model=fallback_model,
            credential_pool=credential_pool,
            providers_allowed=pr.get("only"),
            providers_ignored=pr.get("ignore"),
            providers_order=pr.get("order"),
            provider_sort=pr.get("sort"),
            openrouter_min_coding_score=(_cfg.get("openrouter") or {}).get("min_coding_score"),
            enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
            disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg),
            quiet_mode=True,
            # Cron jobs should always inherit the user's SOUL.md identity from
            # HERMES_HOME. When a workdir is configured, also inject project
            # context files (AGENTS.md / CLAUDE.md / .cursorrules) from there.
            # Without a workdir, keep cwd context discovery disabled.
            skip_context_files=not bool(_job_workdir),
            load_soul_identity=True,
            skip_memory=True,  # Cron system prompts would corrupt user representations
            platform="cron",
            session_id=_cron_session_id,
            session_db=_session_db,
        )
        
        # Run the agent with an *inactivity*-based timeout: the job can run
        # for hours if it's actively calling tools / receiving stream tokens,
        # but a hung API call or stuck tool with no activity for the configured
        # duration is caught and killed.  Default 600s (10 min inactivity);
        # override via HERMES_CRON_TIMEOUT env var.  0 = unlimited.
        #
        # Uses the agent's built-in activity tracker (updated by
        # _touch_activity() on every tool call, API call, and stream delta).
        _raw_cron_timeout = os.getenv("HERMES_CRON_TIMEOUT", "").strip()
        if _raw_cron_timeout:
            try:
                _cron_timeout = float(_raw_cron_timeout)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid HERMES_CRON_TIMEOUT=%r; using default 600s",
                    _raw_cron_timeout,
                )
                _cron_timeout = 600.0
        else:
            _cron_timeout = 600.0
        _cron_inactivity_limit = _cron_timeout if _cron_timeout > 0 else None
        _POLL_INTERVAL = 5.0
        _cron_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Preserve scheduler-scoped ContextVar state (for example skill-declared
        # env passthrough registrations) when the cron run hops into the worker
        # thread used for inactivity timeout monitoring.
        _cron_context = contextvars.copy_context()
        _cron_future = _cron_pool.submit(_cron_context.run, agent.run_conversation, prompt)
        _inactivity_timeout = False
        try:
            if _cron_inactivity_limit is None:
                # Unlimited — just wait for the result.
                result = _cron_future.result()
            else:
                result = None
                while True:
                    done, _ = concurrent.futures.wait(
                        {_cron_future}, timeout=_POLL_INTERVAL,
                    )
                    if done:
                        result = _cron_future.result()
                        break
                    # Agent still running — check inactivity.
                    _idle_secs = 0.0
                    if hasattr(agent, "get_activity_summary"):
                        try:
                            _act = agent.get_activity_summary()
                            _idle_secs = _act.get("seconds_since_activity", 0.0)
                        except Exception:
                            pass
                    if _idle_secs >= _cron_inactivity_limit:
                        _inactivity_timeout = True
                        break
        except Exception:
            _cron_pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            _cron_pool.shutdown(wait=False, cancel_futures=True)

        if _inactivity_timeout:
            # Build diagnostic summary from the agent's activity tracker.
            _activity = {}
            if hasattr(agent, "get_activity_summary"):
                try:
                    _activity = agent.get_activity_summary()
                except Exception:
                    pass
            _last_desc = _activity.get("last_activity_desc", "unknown")
            _secs_ago = _activity.get("seconds_since_activity", 0)
            _cur_tool = _activity.get("current_tool")
            _iter_n = _activity.get("api_call_count", 0)
            _iter_max = _activity.get("max_iterations", 0)

            logger.error(
                "Job '%s' idle for %.0fs (inactivity limit %.0fs) "
                "| last_activity=%s | iteration=%s/%s | tool=%s",
                job_name, _secs_ago, _cron_inactivity_limit,
                _last_desc, _iter_n, _iter_max,
                _cur_tool or "none",
            )
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (inactivity)")
            raise TimeoutError(
                f"Cron job '{job_name}' idle for "
                f"{int(_secs_ago)}s (limit {int(_cron_inactivity_limit)}s) "
                f"— last activity: {_last_desc}"
            )

        # Guard against non-dict returns from run_conversation under error conditions
        if not isinstance(result, dict):
            raise RuntimeError(
                f"agent.run_conversation returned {type(result).__name__} instead of dict: {result!r}"
            )

        # If the agent itself reported failure (e.g. all retries exhausted on
        # API errors, model abort, mid-run interrupt), do not silently mark the
        # job as successful. run_agent populates `failed=True`/`completed=False`
        # on these paths and may put the error into `final_response`, which
        # would otherwise be delivered as if it were the agent's reply and the
        # job's `last_status` set to "ok". Raise so the except handler below
        # builds the proper failure tuple. (issue #17855)
        turn_exit_reason = str(result.get("turn_exit_reason") or "")
        final_response_text = (result.get("final_response") or "").strip()
        max_iteration_summary = (
            result.get("failed") is not True
            and result.get("completed") is False
            and turn_exit_reason.startswith("max_iterations_reached(")
            and bool(final_response_text)
        )
        if result.get("failed") is True or (result.get("completed") is False and not max_iteration_summary):
            _err_text = (
                result.get("error")
                or final_response_text
                or "agent reported failure"
            )
            raise RuntimeError(_err_text)
        if max_iteration_summary:
            logger.warning(
                "Job '%s' reached the iteration limit but produced a final fallback response; "
                "delivering the response instead of failing the cron run",
                job_name,
            )

        final_response = result.get("final_response", "") or ""
        # Strip leaked placeholder text that upstream may inject on empty completions.
        if final_response.strip() == "(No response generated)":
            final_response = ""
        # Cron silence on abnormal empty turns.  The turn-completion explainer
        # (#34452) replaces a blank/empty model turn with a "⚠️ No reply: …"
        # string so interactive surfaces (CLI/gateway) explain why the box is
        # empty.  In a cron context that turns a previously-silent empty turn
        # into a delivered warning (Manfredi's Telegram symptom).  Detect the
        # explainer text deterministically (via the same formatter that
        # produced it) and treat it as empty so the empty-response suppression
        # and soft-failure marking below apply — restoring pre-#34452 silence
        # for scheduled jobs without disabling the explainer everywhere.
        if final_response.strip() and turn_exit_reason:
            try:
                _explainer_text = AIAgent._format_turn_completion_explanation(turn_exit_reason)
            except Exception:
                _explainer_text = ""
            if _explainer_text and final_response.strip() == _explainer_text.strip():
                logger.info(
                    "Job '%s': abnormal empty turn (%s) — suppressing explainer for cron delivery",
                    job_id,
                    turn_exit_reason,
                )
                final_response = ""
        # Use a separate variable for log display; keep final_response clean
        # for delivery logic (empty response = no delivery).
        logged_response = final_response if final_response else "(No response generated)"
        
        output = f"""# Cron Job: {job_name}

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Response

{logged_response}
"""
        
        logger.info("Job '%s' completed successfully", job_name)
        return True, output, final_response, None
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.exception("Job '%s' failed: %s", job_name, error_msg)
        
        output = f"""# Cron Job: {job_name} (FAILED)

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Error

```
{error_msg}
```
"""
        return False, output, "", error_msg

    finally:
        # Restore TERMINAL_CWD to whatever it was before this job ran.  We
        # only ever mutate it when the job has a workdir; see the setup block
        # at the top of run_job for the serialization guarantee.
        if _job_workdir:
            if _prior_terminal_cwd == "_UNSET_":
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = _prior_terminal_cwd
        # Release the cwd lock now that the env is restored, so a waiting
        # workdir job (or queued reader) can proceed without seeing the override.
        if _holds_cwd_write:
            _terminal_cwd_lock.release_write()
        else:
            _terminal_cwd_lock.release_read()
        # Clean up ContextVar session/delivery state for this job.
        clear_session_vars(_ctx_tokens)
        for _var_name in _cron_delivery_vars:
            _VAR_MAP[_var_name].set("")
        if _session_db:
            # Title the cron session from the job (name → short prompt → id) so
            # sidebars/history show a meaningful label instead of the injected
            # "[IMPORTANT: …]" hint that is the session's first message. Set here
            # (not at create time) so the agent's own INSERT keeps model /
            # system_prompt; this only UPDATEs the title column. The run-time
            # suffix keeps it unique against the sessions.title index across runs.
            try:
                _title_base = " ".join(job_name.split())[:60].strip() or f"cron {job_id}"
                _cron_title = f"{_title_base} · {_hermes_now().strftime('%b %d %H:%M')}"
                _session_db.set_session_title(_cron_session_id, _cron_title)
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to set cron session title: %s", job_id, e)
            try:
                _session_db.end_session(_cron_session_id, "cron_complete")
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to end session: %s", job_id, e)
            try:
                _session_db.close()
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to close SQLite session store: %s", job_id, e)
        # Release subprocesses, terminal sandboxes, browser daemons, and the
        # main OpenAI/httpx client held by this ephemeral cron agent. Without
        # this, a gateway that ticks cron every N minutes leaks fds per job
        # until it hits EMFILE (#10200 / "too many open files").
        #
        # When the caller opted to defer teardown (passed a list), hand the live
        # agent back instead of closing it here — delivery must run against a
        # live async client, and the caller tears down afterwards (#58720).
        if defer_agent_teardown is not None:
            if agent is not None:
                defer_agent_teardown.append(agent)
        else:
            _teardown_cron_agent(agent, job_id)


def _teardown_cron_agent(agent, job_id: str) -> None:
    """Release an ephemeral cron agent's async resources.

    Split out of ``run_job``'s ``finally`` so a caller that defers teardown
    (to deliver first — #58720) can invoke the identical cleanup AFTER delivery.
    Closes the agent (subprocesses, sandboxes, browser daemons, OpenAI/httpx
    client) and reaps stale async clients whose loop has since closed. Idempotent
    and independently guarded, matching the original inline behavior.
    """
    try:
        if agent is not None:
            agent.close()
    except (Exception, KeyboardInterrupt) as e:
        logger.debug("Job '%s': failed to close agent resources: %s", job_id, e)
    # Each cron run spins up a short-lived worker thread whose event loop
    # dies as soon as the ``ThreadPoolExecutor`` shuts down. Any async
    # httpx clients cached under that loop are now unusable — reap them
    # so their transports don't accumulate in the process-global cache.
    try:
        from agent.auxiliary_client import cleanup_stale_async_clients
        cleanup_stale_async_clients()
    except Exception as e:
        logger.debug("Job '%s': failed to reap stale auxiliary clients: %s", job_id, e)


def run_one_job(job: dict, *, adapters=None, loop=None, verbose: bool = False) -> bool:
    """Run ONE due job end-to-end: execute → save output → deliver → mark.

    This is the shared firing body extracted from ``tick``'s per-job closure so
    that BOTH the built-in ticker and an external provider's ``fire_due`` (e.g.
    Chronos) run the identical sequence — no duplicated correctness.

    It does NOT decide whether the job is due, claim it, or compute the next
    run — those are the caller's concern (``tick`` advances ``next_run_at``
    under the file lock before dispatch; an external provider claims via the
    store CAS). This function only fires the given job once.

    Returns True if the job was processed (even if the job itself failed —
    failure is recorded via ``mark_job_run``), False only if processing raised.
    """
    try:
        # Pre-run dispatch claim (issue #38758): atomically commit a finite
        # one-shot's dispatch BEFORE its side effect runs, so a tick that dies
        # mid-execution (gateway kill, OOM, segfault, hard-timeout) cannot
        # re-fire the job forever on restart. No-op for recurring jobs (they
        # use advance_next_run) and infinite/no-repeat jobs. This lives here in
        # the shared body so BOTH the built-in ticker and the external provider
        # (Chronos fire_due) get at-most-times semantics.
        if not claim_dispatch(job["id"]):
            logger.info(
                "Job '%s': one-shot dispatch limit reached — skipping",
                job.get("name", job["id"]),
            )
            return True  # not an error — already handled/removed

        # Run the job under the profile's secret scope. get_secret() fails
        # closed outside a scope once profile isolation is in play (multiple
        # gateway profiles / room→profile multiplexing), and cron fires from
        # the ticker thread where no per-turn scope is installed — so
        # resolve_runtime_provider() raised UnscopedSecretError before model
        # selection, breaking every cron job. Mirrors the per-turn pattern in
        # gateway/run.py (_profile_runtime_scope).
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )

        _scope_token = set_secret_scope(
            build_profile_secret_scope(_get_hermes_home())
        )
        # Defer the cron agent's async-resource teardown until AFTER delivery.
        # run_job normally closes the agent (and reaps stale async clients) in
        # its finally block; doing that before _deliver_result runs means the
        # live send races a torn-down async client (#58720). Passing a holder
        # list makes run_job hand the agent back instead, and we tear it down
        # below once delivery is done. Defense-in-depth alongside the
        # interpreter-shutdown guard in _deliver_result.
        _deferred_agents: list = []
        _escalated_run = False
        try:
            success, output, final_response, error = run_job(
                job, defer_agent_teardown=_deferred_agents
            )

            # Automatic escalation for downshifted (economy-tier) jobs: a
            # failed or empty run is retried ONCE on the model the job ran
            # on before the downshift (or the config default when it was
            # unpinned). Bookkeeping + auto-pin happens after mark_job_run
            # below. Guarded so escalation can never break normal firing.
            try:
                from cron import economy as _economy
                _initial_failed = (not success) or not (final_response or "").strip()
                if _initial_failed and not job.get("no_agent") and _economy.is_downshifted(job):
                    _esc_provider, _esc_model = _economy.escalation_target(job)
                    _esc_job = dict(job)
                    # Restore BOTH pre-downshift pins exactly: None means the
                    # job was unpinned, so the retry resolves the normal
                    # config default — never the economy job's provider.
                    _esc_job["model"] = _esc_model
                    _esc_job["provider"] = _esc_provider
                    logger.warning(
                        "Job '%s': economy-tier run failed (%s) — retrying once "
                        "on %s", job["id"], error or "empty response",
                        _esc_model or "config default model",
                    )
                    _escalated_run = True
                    success, output, final_response, error = run_job(
                        _esc_job, defer_agent_teardown=_deferred_agents
                    )
            except Exception as _esc_exc:
                logger.error(
                    "Job '%s': economy escalation retry failed: %s",
                    job["id"], _esc_exc,
                )
        except BaseException:
            # run_job's finally still hands back the agent when it raises; tear
            # it down here so a failed run never leaks its async resources
            # (#10200), then re-raise into the outer handler. BaseException
            # (not just Exception) so a KeyboardInterrupt/SystemExit mid-run
            # still triggers teardown before propagating.
            for _deferred_agent in _deferred_agents:
                _teardown_cron_agent(_deferred_agent, job["id"])
            raise
        finally:
            reset_secret_scope(_scope_token)

        # Everything from here through delivery runs with the agent still live
        # (deferred teardown). Wrap it ALL in a try/finally so that if any step
        # between run_job returning and delivery — save_job_output, the [SILENT]
        # / empty-response computation, or _deliver_result itself — raises, the
        # deferred agent is still torn down. Otherwise the outer `except` would
        # swallow the error and leak the agent's subprocesses/clients (#10200).
        delivery_error = None
        try:
            output_file = save_job_output(job["id"], output)
            if verbose:
                logger.info("Output saved to: %s", output_file)

            # Deliver the final response to the origin/target chat.
            # If the agent responded with [SILENT], skip delivery (but
            # output is already saved above).  Failed jobs always deliver.
            deliver_content = final_response if success else _summarize_cron_failure_for_delivery(job, error)
            # Treat whitespace-only final responses the same as empty
            # responses: do not deliver a blank message, and let the
            # empty-response guard below mark the run as a soft failure.
            should_deliver = bool(deliver_content.strip())
            # Cron silence suppression — see _is_cron_silence_response.  Replaces the
            # old `SILENT_MARKER in ...upper()` substring check, which both leaked
            # bracketless near-markers ("SILENT" / "NO_REPLY") and wrongly swallowed
            # a real report that merely quoted "[SILENT]" mid-sentence (#51438,
            # #46917).  Keeps the intentional bracketed-prefix / trailing-line
            # tolerance the cron contract relies on.
            if should_deliver and success and _is_cron_silence_response(deliver_content):
                logger.info("Job '%s': agent returned %s — skipping delivery", job["id"], SILENT_MARKER)
                should_deliver = False

            if should_deliver:
                try:
                    delivery_error = _deliver_result(job, deliver_content, adapters=adapters, loop=loop)
                except Exception as de:
                    delivery_error = str(de)
                    logger.error("Delivery failed for job %s: %s", job["id"], de)
        finally:
            # Tear down the deferred agent(s) now that save + delivery have run
            # (or raised). Must happen on every path so cron agents never leak
            # their subprocesses/clients (#10200).
            for _deferred_agent in _deferred_agents:
                _teardown_cron_agent(_deferred_agent, job["id"])

        # Treat empty final_response as a soft failure so last_status
        # is not "ok" — the agent ran but produced nothing useful.
        # (issue #8585)
        if success and not final_response.strip():
            success = False
            error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

        mark_job_run(job["id"], success, error, delivery_error=delivery_error)
        _log_cron_run_to_discord(job, success, error, adapters=adapters, loop=loop)

        # Economy-tier bookkeeping: record this run's outcome on downshifted
        # jobs and auto-pin back to the strong model after repeated
        # escalations, notifying the owner. Best-effort — must never affect
        # the run result.
        try:
            from cron import economy as _economy
            if _economy.is_downshifted(job):
                _should_pin = _economy.record_run_outcome(
                    job["id"], escalated=_escalated_run, success=success,
                )
                if _should_pin:
                    _pin = _economy.revert_downshift(
                        job["id"],
                        reason=(
                            f"{_economy.AUTO_PIN_ESCALATIONS} of the last "
                            f"{_economy.AUTO_PIN_WINDOW} runs needed escalation"
                        ),
                        source="auto",
                    )
                    if _pin:
                        _notice = (
                            f"⚠️ Cron '{_pin['name']}' has been moved back to "
                            f"{_pin['new_model']} — the economy model "
                            f"({_pin['old_model']}) needed escalation on "
                            f"{_economy.AUTO_PIN_ESCALATIONS} of its last "
                            f"{_economy.AUTO_PIN_WINDOW} runs. It will stay on "
                            "the stronger model until you downshift it again."
                        )
                        try:
                            _deliver_result(job, _notice, adapters=adapters, loop=loop)
                        except Exception as _nde:
                            logger.warning(
                                "Job '%s': auto-pin notice delivery failed: %s",
                                job["id"], _nde,
                            )
        except Exception as _eco_exc:
            logger.error(
                "Job '%s': economy bookkeeping failed: %s", job["id"], _eco_exc,
            )
        return True

    except Exception as e:
        logger.error("Error processing job %s: %s", job['id'], e)
        mark_job_run(job["id"], False, str(e))
        _log_cron_run_to_discord(job, False, str(e), adapters=adapters, loop=loop)
        return False


def _log_cron_run_to_discord(job: dict, success: bool, error, *, adapters=None, loop=None) -> None:
    """LOCAL feature: append this run to the pinned cron-history Discord
    thread. Best-effort and fire-and-forget — a posting failure must never
    affect job execution or marking."""
    try:
        from cron.discord_cron_threads import log_run
        log_run(job, success, error, adapters=adapters, loop=loop)
    except Exception as e:
        logger.debug("cron-history Discord log skipped: %s", e)


def _notify_provider_jobs_changed() -> None:
    """Best-effort: tell the active scheduler provider the job set changed.

    Called by the consumer surfaces (model tool / CLI / REST) AFTER a
    successful store mutation (create/update/remove/pause/resume) so an external
    provider (Chronos) can re-provision/cancel the affected one-shot via NAS.
    No-op for the built-in (it re-reads jobs.json each tick), so the default
    path is unchanged. Lives here (not in cron/jobs.py) to keep the store free
    of provider imports — avoids an import cycle and keeps jobs.py low-coupling.
    Never raises into the caller.
    """
    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        resolve_cron_scheduler().on_jobs_changed()
    except Exception as e:
        logger.debug("on_jobs_changed notify failed: %s", e)
    # LOCAL feature: reflect the mutation in the pinned cron-jobs Discord
    # thread immediately (in-gateway callers only; standalone CLI processes
    # have no cached adapter/loop and rely on the next ticker sync).
    try:
        from cron.discord_cron_threads import request_sync
        request_sync()
    except Exception as e:
        logger.debug("Discord cron-threads request_sync failed: %s", e)


def tick(verbose: bool = True, adapters=None, loop=None, sync: bool = True) -> int:
    """
    Check and run all due jobs.
    
    Uses a file lock so only one tick runs at a time, even if the gateway's
    in-process ticker and a standalone daemon or manual tick overlap.
    
    Args:
        verbose: Whether to print status messages
        adapters: Optional dict mapping Platform → live adapter (from gateway)
        loop: Optional asyncio event loop (from gateway) for live adapter sends
    
    Returns:
        Number of jobs executed (0 if another tick is already running)
    """
    lock_dir, lock_file = _get_lock_paths()
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Cross-platform file locking: fcntl on Unix, msvcrt on Windows
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w", encoding="utf-8")
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        logger.debug("Tick skipped — another instance holds the lock")
        if lock_fd is not None:
            lock_fd.close()
        return 0

    try:
        # LOCAL feature: keep the pinned cron-jobs/cron-history Discord threads
        # in sync. Fire-and-forget on the gateway loop — never blocks the tick.
        try:
            from cron.discord_cron_threads import schedule_sync as _discord_threads_sync
            _discord_threads_sync(adapters, loop)
        except Exception as _dte:
            logger.debug("Discord cron-threads sync skipped: %s", _dte)

        due_jobs = get_due_jobs()

        if verbose and not due_jobs:
            logger.info("%s - No jobs due", _hermes_now().strftime('%H:%M:%S'))
            return 0

        if verbose:
            logger.info("%s - %s job(s) due", _hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        # Advance next_run_at for all recurring jobs FIRST, under the file lock,
        # before any execution begins.  This preserves at-most-once semantics.
        # For parallel jobs that are already running, advance_next_run keeps
        # bumping next_run_at forward so the grace window never expires.
        # mark_job_run() overwrites next_run_at on completion.
        for job in due_jobs:
            advance_next_run(job["id"])

        # Resolve max parallel workers: env var > config.yaml > unbounded.
        # Set HERMES_CRON_MAX_PARALLEL=1 to restore old serial behaviour.
        _max_workers: Optional[int] = None
        try:
            _env_par = os.getenv("HERMES_CRON_MAX_PARALLEL", "").strip()
            if _env_par:
                _max_workers = int(_env_par) or None
        except (ValueError, TypeError):
            logger.warning("Invalid HERMES_CRON_MAX_PARALLEL value; defaulting to unbounded")
        if _max_workers is None:
            try:
                _ucfg = load_config() or {}
                _cfg_par = (
                    _ucfg.get("cron", {}) if isinstance(_ucfg, dict) else {}
                ).get("max_parallel_jobs")
                if _cfg_par is not None:
                    _max_workers = int(_cfg_par) or None
            except Exception:
                pass

        if verbose:
            logger.info(
                "Running %d job(s) in parallel (max_workers=%s)",
                len(due_jobs),
                _max_workers if _max_workers else "unbounded",
            )

        def _process_job(job: dict) -> bool:
            """Run one due job end-to-end. Thin wrapper around the shared
            module-level ``run_one_job`` so ``tick`` and external providers
            (Chronos ``fire_due``) use the identical execute→save→deliver→mark
            body."""
            return run_one_job(job, adapters=adapters, loop=loop, verbose=verbose)

        # Partition due jobs: those with a per-job workdir mutate
        # os.environ["TERMINAL_CWD"] inside run_job, which is process-global, so
        # they queue on the single-thread sequential pool to run one at a time.
        # That alone only keeps workdir jobs from overlapping EACH OTHER;
        # run_job's _terminal_cwd_lock is what additionally stops a concurrently
        # firing workdir-less parallel-pool job from observing the override.
        sequential_jobs = [j for j in due_jobs if (j.get("workdir") or "").strip()]
        parallel_jobs = [j for j in due_jobs if not (j.get("workdir") or "").strip()]

        _results: list = []
        _all_futures: list = []

        def _submit_with_guard(job: dict, pool: concurrent.futures.ThreadPoolExecutor):
            """Submit a job fire-and-forget with the in-flight dedup guard.

            Returns the future, or None if the job was skipped because a prior
            tick's run of the same job is still in flight.  The running-set
            membership is released in the worker's finally block.
            """
            job_id = job["id"]
            # A tick can race gateway teardown: once the interpreter is
            # finalizing, ``pool.submit`` raises "cannot schedule new futures
            # after interpreter shutdown" and crashes the tick. Skip cleanly —
            # the job stays due and will fire on the next healthy tick
            # (#58720, #55924).
            if _interpreter_shutting_down():
                logger.warning(
                    "Job '%s' not dispatched — interpreter is shutting down",
                    job.get("name", job_id),
                )
                return None
            with _running_lock:
                if job_id in _running_job_ids:
                    logger.info("Job '%s' already running — skipping", job.get("name", job_id))
                    return None
                _running_job_ids.add(job_id)
            _ctx = contextvars.copy_context()

            def _run_and_release(j=job, ctx=_ctx):
                try:
                    return ctx.run(_process_job, j)
                finally:
                    with _running_lock:
                        _running_job_ids.discard(j["id"])

            try:
                return pool.submit(_run_and_release)
            except RuntimeError as submit_err:
                # Interpreter began finalizing between the guard above and the
                # submit — release the in-flight claim we just took and skip.
                if _interpreter_shutting_down(submit_err):
                    with _running_lock:
                        _running_job_ids.discard(job_id)
                    logger.warning(
                        "Job '%s' not dispatched — interpreter is shutting down",
                        job.get("name", job_id),
                    )
                    return None
                raise

        # Sequential pass for env-mutating (workdir) jobs.
        # Queued to a persistent single-thread pool so they run one at a time
        # WITHOUT blocking the ticker thread — a long workdir job no
        # longer starves the rest of the schedule (same fix as the parallel
        # pass, just serialized).  The in-flight guard prevents a still-running
        # job from being re-queued on the next tick.
        if sequential_jobs:
            seq_pool = _get_sequential_pool()
            for job in sequential_jobs:
                fut = _submit_with_guard(job, seq_pool)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)  # optimistically counted

        # Parallel pass — persistent pool, non-blocking dispatch.
        # Jobs that are already running (from a previous tick) are skipped.
        # mark_job_run() updates next_run_at on completion, so the next tick
        # after completion finds the job due again naturally.  No catch-up
        # queue needed.
        if parallel_jobs:
            pool = _get_parallel_pool(_max_workers)
            for job in parallel_jobs:
                fut = _submit_with_guard(job, pool)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)  # optimistically counted

        # Best-effort sweep of MCP stdio subprocesses that survived their
        # session teardown.  Must run AFTER jobs finish so active sessions
        # (including live user chats) are never touched — only PIDs explicitly
        # detected as orphans in tools.mcp_tool._run_stdio's finally block are
        # reaped.
        def _sweep_mcp_orphans() -> None:
            try:
                from tools.mcp_tool import _kill_orphaned_mcp_children
                _kill_orphaned_mcp_children()
            except Exception as _e:
                logger.debug("Post-tick MCP orphan cleanup failed: %s", _e)

        if sync:
            # Sync mode (tests / manual ticks): wait for all dispatched jobs,
            # collect results, then sweep once.
            for f in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(f.result())
                except Exception as exc:
                    logger.error("Cron job future failed: %s", exc)
                    _results.append(False)
            _sweep_mcp_orphans()
            return sum(_results)

        # Async (gateway ticker) mode: don't block.  Sweep orphans via a
        # done-callback fired after the LAST dispatched job completes, so the
        # sweep still happens after jobs finish without stalling the tick.
        if _all_futures:
            _remaining = [len(_all_futures)]

            def _on_done(_f: concurrent.futures.Future) -> None:
                _remaining[0] -= 1
                try:
                    _exc = _f.exception()
                    if _exc is not None:
                        logger.error("Cron job future failed in async mode: %s", _exc, exc_info=(type(_exc), _exc, _exc.__traceback__))
                except Exception:
                    pass
                if _remaining[0] <= 0:
                    _sweep_mcp_orphans()

            for _f in _all_futures:
                _f.add_done_callback(_on_done)
        else:
            # Nothing dispatched (all skipped / no due jobs) — sweep inline.
            _sweep_mcp_orphans()

        return sum(_results)
    finally:
        if fcntl:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        lock_fd.close()


if __name__ == "__main__":
    tick(verbose=True)
