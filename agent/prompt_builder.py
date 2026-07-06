"""System prompt assembly -- identity, platform hints, skills index, context files.

All functions are stateless. AIAgent._build_system_prompt() calls these to
assemble pieces, then combines them with memory and ephemeral prompts.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context file scanning — detect prompt injection in AGENTS.md, .cursorrules,
# SOUL.md before they get injected into the system prompt.
# ---------------------------------------------------------------------------

_CONTEXT_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'].*display\s*:\s*none', "hidden_div"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', "translate_execute"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', "read_secrets"),
]

_CONTEXT_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


def _scan_context_content(content: str, filename: str) -> str:
    """Scan context file content for injection. Returns sanitized content."""
    findings = []

    # Check invisible unicode
    for char in _CONTEXT_INVISIBLE_CHARS:
        if char in content:
            findings.append(f"invisible unicode U+{ord(char):04X}")

    # Check threat patterns
    for pattern, pid in _CONTEXT_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)

    if findings:
        logger.warning("Context file %s blocked: %s", filename, ", ".join(findings))
        return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"

    return content

# =========================================================================
# Constants
# =========================================================================

DEFAULT_AGENT_IDENTITY = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

HERMES_AGENT_HELP_GUIDANCE = (
    "You run on Hermes Agent (by Nous Research). When the user needs help with "
    "Hermes itself — configuring, setting up, using, extending, or troubleshooting "
    "it — or when you need to understand your own features, tools, or capabilities, "
    "the documentation at https://hermes-agent.nousresearch.com/docs is your "
    "authoritative reference and always holds the latest, most up-to-date "
    "information. Load the `hermes-agent` skill with skill_view(name='hermes-agent') "
    "for additional guidance and proven workflows, but treat the docs as the source "
    "of truth when the two differ."
)

MEMORY_GUIDANCE = (
    "You have persistent memory across sessions. Save durable facts using the memory "
    "tool: user preferences, environment details, tool quirks, and stable conventions. "
    "Memory is injected into every turn, so keep it compact and focused on facts that "
    "will still matter later.\n"
    "Prioritize what reduces future user steering — the most valuable memory is one "
    "that prevents the user from having to correct or remind you again. "
    "User preferences and recurring corrections matter more than procedural task details.\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state to memory; use session_search to recall those from past transcripts. "
    "Specifically: do not record PR numbers, issue numbers, commit SHAs, 'fixed bug X', "
    "'submitted PR Y', 'Phase N done', file counts, or any artifact that will be stale "
    "in 7 days. If a fact will be stale in a week, it does not belong in memory. "
    "If you've discovered a new way to do something, solved a problem that could be "
    "necessary later, save it as a skill with the skill tool.\n"
    "Write memories as declarative facts, not instructions to yourself. "
    "'User prefers concise responses' ✓ — 'Always respond concisely' ✗. "
    "'Project uses pytest with xdist' ✓ — 'Run tests with pytest -n 4' ✗. "
    "Imperative phrasing gets re-read as a directive in later sessions and can "
    "cause repeated work or override the user's current request. Procedures and "
    "workflows belong in skills, not memory."
)

SESSION_SEARCH_GUIDANCE = (
    "When the user references something from a past conversation or you suspect "
    "relevant cross-session context exists, use session_search to recall it before "
    "asking them to repeat themselves."
)

SKILLS_GUIDANCE = (
    "After completing a complex task (5+ tool calls), fixing a tricky error, "
    "or discovering a non-trivial workflow, save the approach as a "
    "skill with skill_manage so you can reuse it next time.\n"
    "When using a skill and finding it outdated, incomplete, or wrong, "
    "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
    "Skills that aren't maintained become liabilities."
)

TOOL_USE_ENFORCEMENT_GUIDANCE = (
    "# Tool-use enforcement\n"
    "You MUST use your tools to take action — do not describe what you would do "
    "or plan to do without actually doing it. When you say you will perform an "
    "action (e.g. 'I will run the tests', 'Let me check the file', 'I will create "
    "the project'), you MUST immediately make the corresponding tool call in the same "
    "response. Never end your turn with a promise of future action — execute it now.\n"
    "Keep working until the task is actually complete. Do not stop with a summary of "
    "what you plan to do next time. If you have tools available that can accomplish "
    "the task, use them instead of telling the user what you would do.\n"
    "Every response should either (a) contain tool calls that make progress, or "
    "(b) deliver a final result to the user. Responses that only describe intentions "
    "without acting are not acceptable."
)

# Model name substrings that trigger tool-use enforcement guidance.
TOOL_USE_ENFORCEMENT_MODELS = ("gpt", "codex", "gemini", "gemma", "grok", "glm", "qwen", "deepseek")

TASK_COMPLETION_GUIDANCE = (
    "# Finishing the job\n"
    "When the user asks you to build, run, or verify something, the deliverable is "
    "a working artifact backed by real tool output — not a description of one. "
    "Do not stop after writing a stub, a plan, or a single command. Keep working "
    "until you have actually exercised the code or produced the requested result, "
    "then report what real execution returned.\n"
    "If a tool, install, or network call fails and blocks the real path, say so "
    "directly and try an alternative (different package manager, different "
    "approach, ask the user). NEVER substitute plausible-looking fabricated "
    "output (made-up data, invented file contents, synthesised API responses) "
    "for results you couldn't actually produce. Reporting a blocker honestly "
    "is always better than inventing a result."
)

PARALLEL_TOOL_CALL_GUIDANCE = (
    "# Parallel tool calls\n"
    "When you need several pieces of information that don't depend on each "
    "other, request them together in a single response instead of one tool "
    "call per turn. Independent reads, searches, web fetches, and read-only "
    "commands should be batched into the same assistant turn — the runtime "
    "executes independent calls concurrently, and batching avoids resending "
    "the whole conversation on every extra round-trip.\n"
    "Only serialize calls when a later call genuinely depends on an earlier "
    "call's result (e.g. you must read a file before you can patch it). When "
    "in doubt and the calls are independent, batch them."
)

OPENAI_MODEL_EXECUTION_GUIDANCE = (
    "# Execution discipline\n"
    "<tool_persistence>\n"
    "- Use tools whenever they improve correctness, completeness, or grounding.\n"
    "- Do not stop early when another tool call would materially improve the result.\n"
    "- If a tool returns empty or partial results, retry with a different query or "
    "strategy before giving up.\n"
    "- Keep calling tools until: (1) the task is complete, AND (2) you have verified "
    "the result.\n"
    "</tool_persistence>\n"
    "\n"
    "<mandatory_tool_use>\n"
    "NEVER answer these from memory or mental computation — ALWAYS use a tool:\n"
    "- Arithmetic, math, calculations → use terminal or execute_code\n"
    "- Hashes, encodings, checksums → use terminal (e.g. sha256sum, base64)\n"
    "- Current time, date, timezone → use terminal (e.g. date)\n"
    "- System state: OS, CPU, memory, disk, ports, processes → use terminal\n"
    "- File contents, sizes, line counts → use read_file, search_files, or terminal\n"
    "- Git history, branches, diffs → use terminal\n"
    "- Current facts (weather, news, versions) → use web_search\n"
    "Your memory and user profile describe the USER, not the system you are "
    "running on. The execution environment may differ from what the user profile "
    "says about their personal setup.\n"
    "</mandatory_tool_use>\n"
    "\n"
    "<act_dont_ask>\n"
    "When a question has an obvious default interpretation, act on it immediately "
    "instead of asking for clarification. Examples:\n"
    "- 'Is port 443 open?' → check THIS machine (don't ask 'open where?')\n"
    "- 'What OS am I running?' → check the live system (don't use user profile)\n"
    "- 'What time is it?' → run `date` (don't guess)\n"
    "Only ask for clarification when the ambiguity genuinely changes what tool "
    "you would call.\n"
    "</act_dont_ask>\n"
    "\n"
    "<prerequisite_checks>\n"
    "- Before taking an action, check whether prerequisite discovery, lookup, or "
    "context-gathering steps are needed.\n"
    "- Do not skip prerequisite steps just because the final action seems obvious.\n"
    "- If a task depends on output from a prior step, resolve that dependency first.\n"
    "</prerequisite_checks>\n"
    "\n"
    "<verification>\n"
    "Before finalizing your response:\n"
    "- Correctness: does the output satisfy every stated requirement?\n"
    "- Grounding: are factual claims backed by tool outputs or provided context?\n"
    "- Formatting: does the output match the requested format or schema?\n"
    "- Safety: if the next step has side effects (file writes, commands, API calls), "
    "confirm scope before executing.\n"
    "</verification>\n"
    "\n"
    "<missing_context>\n"
    "- If required context is missing, do NOT guess or hallucinate an answer.\n"
    "- Use the appropriate lookup tool when missing information is retrievable "
    "(search_files, web_search, read_file, etc.).\n"
    "- Ask a clarifying question only when the information cannot be retrieved by tools.\n"
    "- If you must proceed with incomplete information, label assumptions explicitly.\n"
    "</missing_context>"
)

GOOGLE_MODEL_OPERATIONAL_GUIDANCE = (
    "# Google model operational directives\n"
    "Follow these operational rules strictly:\n"
    "- **Absolute paths:** Always construct and use absolute file paths for all "
    "file system operations. Combine the project root with relative paths.\n"
    "- **Verify first:** Use read_file/search_files to check file contents and "
    "project structure before making changes. Never guess at file contents.\n"
    "- **Dependency checks:** Never assume a library is available. Check "
    "package.json, requirements.txt, Cargo.toml, etc. before importing.\n"
    "- **Conciseness:** Keep explanatory text brief — a few sentences, not "
    "paragraphs. Focus on actions and results over narration.\n"
    "- **Non-interactive commands:** Use flags like -y, --yes, --non-interactive "
    "to prevent CLI tools from hanging on prompts.\n"
    "- **Keep going:** Work autonomously until the task is fully resolved. "
    "Don't stop with a plan — execute it.\n"
)

PLATFORM_HINTS = {
    "whatsapp": (
        "You are on a text messaging communication platform, WhatsApp. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. The file "
        "will be sent as a native WhatsApp attachment — images (.jpg, .png, "
        ".webp) appear as photos, videos (.mp4, .mov) play inline, and other "
        "files arrive as downloadable documents. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as photos."
    ),
    "telegram": (
        "You are on a text messaging communication platform, Telegram. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. Images "
        "(.png, .jpg, .webp) appear as photos, audio (.ogg) sends as voice "
        "bubbles, and videos (.mp4) play inline. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as native photos."
    ),
    "discord": (
        "You are in a Discord server or group chat communicating with your user. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.png, .jpg, .webp) are sent as photo "
        "attachments, audio as file attachments. You can also include image URLs "
        "in markdown format ![alt](url) and they will be sent as attachments."
    ),
    "slack": (
        "You are in a Slack workspace communicating with your user. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.png, .jpg, .webp) are uploaded as photo "
        "attachments, audio as file attachments. You can also include image URLs "
        "in markdown format ![alt](url) and they will be uploaded as attachments."
    ),
    "signal": (
        "You are on a text messaging communication platform, Signal. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. Images "
        "(.png, .jpg, .webp) appear as photos, audio as attachments, and other "
        "files arrive as downloadable documents. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as photos."
    ),
    "email": (
        "You are communicating via email. Write clear, well-structured responses "
        "suitable for email. Use plain text formatting (no markdown). "
        "Keep responses concise but complete. You can send file attachments — "
        "include MEDIA:/absolute/path/to/file in your response. The subject line "
        "is preserved for threading. Do not include greetings or sign-offs unless "
        "contextually appropriate."
    ),
    "cli": (
        "You are a CLI AI Agent. Try not to use markdown but simple text "
        "renderable inside a terminal."
    ),
}

CONTEXT_FILE_MAX_CHARS = 20_000
CONTEXT_TRUNCATE_HEAD_RATIO = 0.7
CONTEXT_TRUNCATE_TAIL_RATIO = 0.2


# =========================================================================
# Skills index
# =========================================================================

def _read_skill_description(skill_file: Path, max_chars: int = 60) -> str:
    """Read the description from a SKILL.md frontmatter, capped at max_chars."""
    try:
        raw = skill_file.read_text(encoding="utf-8")[:2000]
        match = re.search(
            r"^---\s*\n.*?description:\s*(.+?)\s*\n.*?^---",
            raw, re.MULTILINE | re.DOTALL,
        )
        if match:
            desc = match.group(1).strip().strip("'\"")
            if len(desc) > max_chars:
                desc = desc[:max_chars - 3] + "..."
            return desc
    except Exception as e:
        logger.debug("Failed to read skill description from %s: %s", skill_file, e)
    return ""


def _skill_is_platform_compatible(skill_file: Path) -> bool:
    """Quick check if a SKILL.md is compatible with the current OS platform.

    Reads just enough to parse the ``platforms`` frontmatter field.
    Skills without the field (the vast majority) are always compatible.
    """
    try:
        from tools.skills_tool import _parse_frontmatter, skill_matches_platform
        raw = skill_file.read_text(encoding="utf-8")[:2000]
        frontmatter, _ = _parse_frontmatter(raw)
        return skill_matches_platform(frontmatter)
    except Exception:
        return True  # Err on the side of showing the skill


def _read_skill_conditions(skill_file: Path) -> dict:
    """Extract conditional activation fields from SKILL.md frontmatter."""
    try:
        from tools.skills_tool import _parse_frontmatter
        raw = skill_file.read_text(encoding="utf-8")[:2000]
        frontmatter, _ = _parse_frontmatter(raw)
        hermes = frontmatter.get("metadata", {}).get("hermes", {})
        return {
            "fallback_for_toolsets": hermes.get("fallback_for_toolsets", []),
            "requires_toolsets": hermes.get("requires_toolsets", []),
            "fallback_for_tools": hermes.get("fallback_for_tools", []),
            "requires_tools": hermes.get("requires_tools", []),
        }
    except Exception:
        return {}


def _skill_should_show(
    conditions: dict,
    available_tools: "set[str] | None",
    available_toolsets: "set[str] | None",
) -> bool:
    """Return False if the skill's conditional activation rules exclude it."""
    if available_tools is None and available_toolsets is None:
        return True  # No filtering info — show everything (backward compat)

    at = available_tools or set()
    ats = available_toolsets or set()

    # fallback_for: hide when the primary tool/toolset IS available
    for ts in conditions.get("fallback_for_toolsets", []):
        if ts in ats:
            return False
    for t in conditions.get("fallback_for_tools", []):
        if t in at:
            return False

    # requires: hide when a required tool/toolset is NOT available
    for ts in conditions.get("requires_toolsets", []):
        if ts not in ats:
            return False
    for t in conditions.get("requires_tools", []):
        if t not in at:
            return False

    return True


def build_skills_system_prompt(
    available_tools: "set[str] | None" = None,
    available_toolsets: "set[str] | None" = None,
) -> str:
    """Build a compact skill index for the system prompt.

    Scans ~/.hermes/skills/ for SKILL.md files grouped by category.
    Includes per-skill descriptions from frontmatter so the model can
    match skills by meaning, not just name.
    Filters out skills incompatible with the current OS platform.
    """
    hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    skills_dir = hermes_home / "skills"

    if not skills_dir.exists():
        return ""

    # Collect skills with descriptions, grouped by category
    # Each entry: (skill_name, description)
    # Supports sub-categories: skills/mlops/training/axolotl/SKILL.md
    # → category "mlops/training", skill "axolotl"
    skills_by_category: dict[str, list[tuple[str, str]]] = {}
    for skill_file in skills_dir.rglob("SKILL.md"):
        # Skip skills incompatible with the current OS platform
        if not _skill_is_platform_compatible(skill_file):
            continue
        # Skip skills whose conditional activation rules exclude them
        conditions = _read_skill_conditions(skill_file)
        if not _skill_should_show(conditions, available_tools, available_toolsets):
            continue
        rel_path = skill_file.relative_to(skills_dir)
        parts = rel_path.parts
        if len(parts) >= 2:
            # Category is everything between skills_dir and the skill folder
            # e.g. parts = ("mlops", "training", "axolotl", "SKILL.md")
            #   → category = "mlops/training", skill_name = "axolotl"
            # e.g. parts = ("github", "github-auth", "SKILL.md")
            #   → category = "github", skill_name = "github-auth"
            skill_name = parts[-2]
            category = "/".join(parts[:-2]) if len(parts) > 2 else parts[0]
        else:
            category = "general"
            skill_name = skill_file.parent.name
        desc = _read_skill_description(skill_file)
        skills_by_category.setdefault(category, []).append((skill_name, desc))

    if not skills_by_category:
        return ""

    # Read category-level descriptions from DESCRIPTION.md
    # Checks both the exact category path and parent directories
    category_descriptions = {}
    for category in skills_by_category:
        cat_path = Path(category)
        desc_file = skills_dir / cat_path / "DESCRIPTION.md"
        if desc_file.exists():
            try:
                content = desc_file.read_text(encoding="utf-8")
                match = re.search(r"^---\s*\n.*?description:\s*(.+?)\s*\n.*?^---", content, re.MULTILINE | re.DOTALL)
                if match:
                    category_descriptions[category] = match.group(1).strip()
            except Exception as e:
                logger.debug("Could not read skill description %s: %s", desc_file, e)

    index_lines = []
    for category in sorted(skills_by_category.keys()):
        cat_desc = category_descriptions.get(category, "")
        if cat_desc:
            index_lines.append(f"  {category}: {cat_desc}")
        else:
            index_lines.append(f"  {category}:")
        # Deduplicate and sort skills within each category
        seen = set()
        for name, desc in sorted(skills_by_category[category], key=lambda x: x[0]):
            if name in seen:
                continue
            seen.add(name)
            if desc:
                index_lines.append(f"    - {name}: {desc}")
            else:
                index_lines.append(f"    - {name}")

    return (
        "## Skills (mandatory)\n"
        "Before replying, scan the skills below. If one clearly matches your task, "
        "load it with skill_view(name) and follow its instructions. "
        "If a skill has issues, fix it with skill_manage(action='patch').\n"
        "\n"
        "<available_skills>\n"
        + "\n".join(index_lines) + "\n"
        "</available_skills>\n"
        "\n"
        "If none match, proceed normally without loading a skill."
    )


# =========================================================================
# Context files (SOUL.md, AGENTS.md, .cursorrules)
# =========================================================================

def _truncate_content(content: str, filename: str, max_chars: int = CONTEXT_FILE_MAX_CHARS) -> str:
    """Head/tail truncation with a marker in the middle."""
    if len(content) <= max_chars:
        return content
    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    marker = f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of {len(content)} chars. Use file tools to read the full file.]\n\n"
    return head + marker + tail


def build_context_files_prompt(cwd: Optional[str] = None) -> str:
    """Discover and load context files for the system prompt.

    Discovery: AGENTS.md (recursive), .cursorrules / .cursor/rules/*.mdc,
    SOUL.md (cwd then ~/.hermes/ fallback). Each capped at 20,000 chars.
    """
    if cwd is None:
        cwd = os.getcwd()

    cwd_path = Path(cwd).resolve()
    sections = []

    # AGENTS.md (hierarchical, recursive)
    top_level_agents = None
    for name in ["AGENTS.md", "agents.md"]:
        candidate = cwd_path / name
        if candidate.exists():
            top_level_agents = candidate
            break

    if top_level_agents:
        agents_files = []
        for root, dirs, files in os.walk(cwd_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', 'venv', '.venv')]
            for f in files:
                if f.lower() == "agents.md":
                    agents_files.append(Path(root) / f)
        agents_files.sort(key=lambda p: len(p.parts))

        total_agents_content = ""
        for agents_path in agents_files:
            try:
                content = agents_path.read_text(encoding="utf-8").strip()
                if content:
                    rel_path = agents_path.relative_to(cwd_path)
                    content = _scan_context_content(content, str(rel_path))
                    total_agents_content += f"## {rel_path}\n\n{content}\n\n"
            except Exception as e:
                logger.debug("Could not read %s: %s", agents_path, e)

        if total_agents_content:
            total_agents_content = _truncate_content(total_agents_content, "AGENTS.md")
            sections.append(total_agents_content)

    # .cursorrules
    cursorrules_content = ""
    cursorrules_file = cwd_path / ".cursorrules"
    if cursorrules_file.exists():
        try:
            content = cursorrules_file.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_context_content(content, ".cursorrules")
                cursorrules_content += f"## .cursorrules\n\n{content}\n\n"
        except Exception as e:
            logger.debug("Could not read .cursorrules: %s", e)

    cursor_rules_dir = cwd_path / ".cursor" / "rules"
    if cursor_rules_dir.exists() and cursor_rules_dir.is_dir():
        mdc_files = sorted(cursor_rules_dir.glob("*.mdc"))
        for mdc_file in mdc_files:
            try:
                content = mdc_file.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_context_content(content, f".cursor/rules/{mdc_file.name}")
                    cursorrules_content += f"## .cursor/rules/{mdc_file.name}\n\n{content}\n\n"
            except Exception as e:
                logger.debug("Could not read %s: %s", mdc_file, e)

    if cursorrules_content:
        cursorrules_content = _truncate_content(cursorrules_content, ".cursorrules")
        sections.append(cursorrules_content)

    # SOUL.md (cwd first, then ~/.hermes/ fallback)
    soul_path = None
    for name in ["SOUL.md", "soul.md"]:
        candidate = cwd_path / name
        if candidate.exists():
            soul_path = candidate
            break
    if not soul_path:
        global_soul = Path.home() / ".hermes" / "SOUL.md"
        if global_soul.exists():
            soul_path = global_soul

    if soul_path:
        try:
            content = soul_path.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_context_content(content, "SOUL.md")
                content = _truncate_content(content, "SOUL.md")
                sections.append(
                    f"## SOUL.md\n\nIf SOUL.md is present, embody its persona and tone. "
                    f"Avoid stiff, generic replies; follow its guidance unless higher-priority "
                    f"instructions override it.\n\n{content}"
                )
        except Exception as e:
            logger.debug("Could not read SOUL.md from %s: %s", soul_path, e)

    if not sections:
        return ""
    return "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n" + "\n".join(sections)
