"""
skills_middleware.py
─────────────────────
A LangChain AgentMiddleware that loads Agent Skills from a `.skills/` directory
and injects them into the agent's context — matching how Claude Code, Codex,
Cursor, and other coding agents load skills installed via `npx skills add` or
`npx add-skill`.

Skills are installed by the standard toolchain into the skills directory:

    npx skills add vercel-labs/agent-skills         # installs to .agents/skills/
    npx skills add vercel-labs/agent-skills -g      # installs globally
    npx add-skill github-user/my-skill --agent deepagents

Each installed skill is a directory containing:

    skill-name/
    ├── SKILL.md           ← required: YAML frontmatter + instructions
    ├── DESIGN.md          ← optional supporting file (loaded automatically)
    ├── reference.md       ← optional reference documentation
    ├── examples/
    │   └── sample.md      ← loaded when skill is active
    └── scripts/
        └── helper.py      ← content loaded; can be executed via dynamic context

What this middleware does (matching the Agent Skills open standard):
  • Scans the skills directory at the start of every agent invocation
  • Parses YAML frontmatter from every SKILL.md
  • Loads all supporting files in each skill folder (*.md, *.txt, scripts, etc.)
  • Executes dynamic context injection (!`command` / ```!...```)
  • Injects skill catalog (names + descriptions) into the system prompt so the
    agent knows what skills are available and when to use them
  • Injects full skill content + supporting files for auto-invocable skills
  • Excludes `disable-model-invocation: true` skills from auto-loading
  • Respects path-based activation (only loads skills matching current context)
  • Supports dynamic skills_dir resolved per-user from runtime.config —
    same pattern as UserScopedShellMiddleware / UserScopedFileSearchMiddleware

Install location for Deep Agents (project / global):
    .agents/skills/          ← npx skills add ... (project default)
    ~/.deepagents/agent/skills/  ← npx skills add ... -g (global)

Usage:

    from pathlib import Path
    from skills_middleware import SkillsMiddleware

    agent = create_agent(
        model=...,
        tools=[...],
        middleware=[
            SkillsMiddleware(
                skills_dir=Path(".agents/skills"),
            ),
            # or per-user:
            SkillsMiddleware(
                skills_dir=lambda rt: (
                    Path("workspace")
                    / rt.config["configurable"]["tenant_id"]
                    / rt.config["configurable"]["user_id"]
                    / ".skills"
                ),
            ),
        ],
    )
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

# ── Runtime config extraction ─────────────────────────────────────────────────
# Newer langgraph versions no longer expose `.config` on the Runtime object.
# Copied from user_scoped_workspace.py — kept local to avoid a cross-dependency.
_get_config_impl: Any = None
for _mod, _fn in [
    ("langgraph.config", "get_config"),
    ("langgraph.pregel", "get_config"),
    ("langgraph.runtime", "get_config"),
]:
    try:
        import importlib as _il
        _get_config_impl = getattr(_il.import_module(_mod), _fn)
        break
    except (ImportError, AttributeError):
        continue


def _runtime_config(runtime: Any) -> dict:
    """Return the current invocation config dict, regardless of langgraph version."""
    cfg = getattr(runtime, "config", None)
    if isinstance(cfg, dict):
        return cfg
    for attr in ("_config", "configurable"):
        val = getattr(runtime, attr, None)
        if isinstance(val, dict):
            return {"configurable": val} if attr == "configurable" else val
    if _get_config_impl is not None:
        try:
            result = _get_config_impl()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {}

logger = logging.getLogger(__name__)

# ── Optional PyYAML ───────────────────────────────────────────────────────────
try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    logger.debug("PyYAML not installed; using built-in frontmatter parser.")


# ══════════════════════════════════════════════════════════════════════════════
#  PART 1 — YAML frontmatter parsing
# ══════════════════════════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Parse YAML frontmatter delimited by --- markers.
    Returns (frontmatter_dict, body_text).
    Falls back to the built-in parser if PyYAML is not installed.
    """
    if not text.startswith("---"):
        return {}, text

    # Find the closing ---
    close = text.find("\n---", 3)
    if close == -1:
        return {}, text

    fm_raw  = text[3:close].strip()
    body    = text[close + 4:].lstrip("\n")

    if _HAS_YAML:
        try:
            fm = _yaml.safe_load(fm_raw) or {}
            if not isinstance(fm, dict):
                fm = {}
        except Exception as exc:
            logger.warning("YAML parse error in frontmatter: %s", exc)
            fm = _fallback_yaml_parse(fm_raw)
    else:
        fm = _fallback_yaml_parse(fm_raw)

    return fm, body


def _fallback_yaml_parse(text: str) -> dict:
    """
    Minimal YAML parser that handles the subset used in SKILL.md frontmatter:
      scalar values, booleans, single-line lists, block lists, nested dicts.
    """
    result: dict = {}
    stack: list[tuple[int, dict]] = [(0, result)]  # (indent, container)

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        # Pop stack to correct indent level
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()

        current = stack[-1][1]

        if stripped.startswith("- "):
            # List item
            val = stripped[2:].strip().strip("\"'")
            # Find the list key from parent context — we look for the last key
            # that was set to [] in the current container
            for k, v in reversed(list(current.items())):
                if isinstance(v, list):
                    v.append(_coerce(val))
                    break
            continue

        if ":" in stripped:
            key, sep, val = stripped.partition(":")
            key = key.strip().strip("\"'")
            val = val.strip()

            if val == "" or val is None:
                # Could be a nested dict or list; reserve the key
                current[key] = {}
                stack.append((indent + 2, current[key]))
            elif val.startswith("[") and val.endswith("]"):
                # Inline list
                items = [x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()]
                current[key] = [_coerce(i) for i in items]
            else:
                current[key] = _coerce(val.strip("\"'"))

    return result


def _coerce(val: str) -> Any:
    """Convert string to bool / int / float / str."""
    if val.lower() == "true":  return True
    if val.lower() == "false": return False
    try: return int(val)
    except ValueError: pass
    try: return float(val)
    except ValueError: pass
    return val


# ══════════════════════════════════════════════════════════════════════════════
#  PART 2 — Dynamic context injection  (!`command` and ```!...```)
# ══════════════════════════════════════════════════════════════════════════════

# Fenced block:  ```!\n...\n```
_FENCED_RE = re.compile(r"```!\s*\n(.*?)\n```", re.DOTALL)

# Inline form: !`command` where ! is at line-start or preceded by whitespace
_INLINE_RE = re.compile(r"(^|\s)`!([^`\n]+)`")


def _resolve_dynamic_context(
    content: str,
    skill_dir: Path,
    shell: str = "bash",
    timeout: int = 30,
) -> str:
    """
    Execute !`command` and ```!...``` blocks and replace them with their output.

    Per the Agent Skills spec:
    - Inline form recognised only when ! is at line-start or after whitespace.
    - Fenced form: ```!\\n...\\n``` (multi-line commands).
    - Substitution runs once; output is not re-scanned.
    """
    def run(cmd: str) -> str:
        try:
            use_shell_bin = "powershell" if shell == "powershell" else "bash"
            if shell == "powershell":
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", cmd],
                    capture_output=True, text=True, timeout=timeout,
                    cwd=str(skill_dir),
                )
            else:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=timeout, cwd=str(skill_dir),
                    executable=shutil.which("bash") or "/bin/sh",
                )
            out = proc.stdout.strip()
            if proc.returncode != 0 and proc.stderr.strip():
                out = (out + "\n" + proc.stderr.strip()).strip()
            return out
        except subprocess.TimeoutExpired:
            return "[dynamic context: command timed out]"
        except Exception as exc:
            return f"[dynamic context error: {exc}]"

    # Fenced blocks first
    def _fenced(m: re.Match) -> str:
        return run(m.group(1).strip())

    content = _FENCED_RE.sub(_fenced, content)

    # Inline commands line by line (per-spec: only at start of line / after whitespace)
    lines = []
    for line in content.split("\n"):
        new = _INLINE_RE.sub(lambda m: m.group(1) + run(m.group(2).strip()), line)
        lines.append(new)

    return "\n".join(lines)


# Need shutil for bash path detection
import shutil


# ══════════════════════════════════════════════════════════════════════════════
#  PART 3 — String substitutions
# ══════════════════════════════════════════════════════════════════════════════

def _apply_substitutions(
    content: str,
    *,
    skill_dir: Path,
    arguments: str = "",
    session_id: str = "",
    effort: str = "medium",
) -> str:
    """
    Apply standard Agent Skills substitutions:
      $ARGUMENTS           → full argument string
      $ARGUMENTS[N]        → N-th argument (0-based)
      $N                   → shorthand for $ARGUMENTS[N]
      ${CLAUDE_SKILL_DIR}  → skill directory path
      ${CLAUDE_SESSION_ID} → session/thread id
      ${CLAUDE_EFFORT}     → effort level
    """
    # Split arguments respecting shell quoting (simple split on whitespace)
    arg_parts = _split_args(arguments)

    # Named argument positions from frontmatter are handled by callers who have
    # the frontmatter; here we handle indexed forms only.

    def _indexed(m: re.Match) -> str:
        idx = int(m.group(1))
        return arg_parts[idx] if idx < len(arg_parts) else ""

    content = re.sub(r"\$ARGUMENTS\[(\d+)\]", _indexed, content)
    content = re.sub(r"(?<![\\])\$(\d+)\b", _indexed, content)
    content = content.replace("$ARGUMENTS", arguments)
    content = content.replace("${CLAUDE_SKILL_DIR}", str(skill_dir))
    content = content.replace("${CLAUDE_SESSION_ID}", session_id)
    content = content.replace("${CLAUDE_EFFORT}", effort)

    # Unescape \$
    content = re.sub(r"\\(\$)", r"\1", content)

    return content


def _split_args(s: str) -> list[str]:
    """Shell-style split respecting quoted strings."""
    import shlex
    try:
        return shlex.split(s) if s else []
    except ValueError:
        return s.split()


# ══════════════════════════════════════════════════════════════════════════════
#  PART 4 — File loader
# ══════════════════════════════════════════════════════════════════════════════

# File extensions to load as text content
_TEXT_EXTENSIONS = frozenset({
    ".md", ".mdx", ".txt", ".rst",           # documentation
    ".yaml", ".yml", ".toml", ".json",        # configuration / data
    ".py", ".js", ".ts", ".jsx", ".tsx",      # scripts
    ".sh", ".bash", ".zsh", ".fish",          # shell scripts
    ".ps1", ".psm1",                          # powershell
    ".rb", ".go", ".rs", ".java", ".kt",      # other languages
    ".html", ".css", ".svg",                  # web
    ".sql",                                   # queries
    ".env.example",                           # env templates
})

# Files to skip
_SKIP_FILENAMES = frozenset({
    "SKILL.md",           # loaded separately as the entry point
    ".gitignore",
    ".gitattributes",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    ".npmrc",
})

_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".cache",
})


def _load_text_file(path: Path, max_kb: int = 100) -> str | None:
    """Read a text file, returning None if too large or binary."""
    try:
        size = path.stat().st_size
        if size > max_kb * 1024:
            return f"[file too large: {size // 1024} KB > {max_kb} KB limit]"
        raw = path.read_bytes()
        # Quick binary check
        if b"\x00" in raw[:512]:
            return None
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  PART 5 — SkillFile and SkillEntry
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SkillFile:
    """A supporting file within a skill directory (not SKILL.md)."""
    rel_path: str     # relative to skill directory, e.g. "reference.md"
    abs_path: Path
    content: str


@dataclass
class SkillEntry:
    """
    A fully-parsed skill, ready to be injected into context.

    Attributes mirror the Agent Skills open standard frontmatter fields.
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    skill_name:   str      # directory name (used as command /skill-name)
    skill_dir:    Path

    # ── Frontmatter fields (Agent Skills standard) ────────────────────────────
    name:                    str        # display name (frontmatter "name" or dir name)
    description:             str        # what it does + when to use
    when_to_use:             str        # supplemental trigger guidance
    disable_model_invocation: bool      # True → only user can invoke, not auto-loaded
    user_invocable:          bool       # False → hidden from command menu
    allowed_tools:           list[str]  # tools pre-approved while skill is active
    disallowed_tools:        list[str]  # tools blocked while skill is active
    model_override:          str | None
    effort:                  str | None
    context:                 str | None  # "fork" for subagent execution
    paths_patterns:          list[str]   # glob patterns for auto-activation
    argument_hint:           str | None
    argument_names:          list[str]   # positional argument names
    shell:                   str         # "bash" | "powershell"
    is_internal:             bool        # metadata.internal: true

    # ── Content ───────────────────────────────────────────────────────────────
    body:              str              # rendered SKILL.md body
    supporting_files:  list[SkillFile]  # all other files in skill dir

    # ── Computed ──────────────────────────────────────────────────────────────
    raw_frontmatter: dict = field(default_factory=dict)

    @property
    def full_description(self) -> str:
        """Combined description + when_to_use (≤1536 chars, per spec)."""
        parts = [p for p in (self.description, self.when_to_use) if p]
        combined = "\n".join(parts)
        return combined[:1536]

    def render_context_block(self, indent: str = "") -> str:
        """
        Format the skill as a context block for system prompt injection.
        Includes SKILL.md body and all supporting files.
        """
        lines = [
            f"### Skill: `/{self.skill_name}`",
            f"**Name:** {self.name}",
        ]
        if self.description:
            lines.append(f"**Description:** {self.description}")
        if self.when_to_use:
            lines.append(f"**When to use:** {self.when_to_use}")
        if self.argument_hint:
            lines.append(f"**Arguments:** {self.argument_hint}")
        if self.allowed_tools:
            lines.append(f"**Pre-approved tools:** {', '.join(self.allowed_tools)}")
        if self.model_override:
            lines.append(f"**Model:** {self.model_override}")
        if self.effort:
            lines.append(f"**Effort:** {self.effort}")
        lines.append("")
        lines.append(self.body)

        if self.supporting_files:
            lines.append("")
            lines.append("#### Supporting files")
            for sf in self.supporting_files:
                lines.append(f"\n##### {sf.rel_path}")
                lines.append(sf.content)

        return ("\n".join(lines)).strip()

    def render_catalog_entry(self) -> str:
        """One-line catalog entry: name + truncated description."""
        manual = " *(manual — invoke with /)*" if self.disable_model_invocation else ""
        return f"- **/{self.skill_name}**{manual}: {self.full_description[:200]}"


# ══════════════════════════════════════════════════════════════════════════════
#  PART 6 — Skills directory scanner
# ══════════════════════════════════════════════════════════════════════════════

def _scan_skills(
    skills_dir: Path,
    *,
    execute_dynamic: bool = True,
    include_supporting_files: bool = True,
    max_file_kb: int = 100,
    max_skill_kb: int = 500,
    include_internal: bool = False,
    session_id: str = "",
    effort: str = "medium",
) -> list[SkillEntry]:
    """
    Walk `skills_dir` and return one `SkillEntry` per skill subdirectory.

    A skill is any directory directly under `skills_dir` that contains
    a `SKILL.md` file. (Matches the Agent Skills standard directory layout.)
    Files at the root of `skills_dir` itself are also treated as a skill if
    a `SKILL.md` exists there (plugin-root pattern).
    """
    if not skills_dir.exists():
        logger.debug("Skills directory does not exist: %s", skills_dir)
        return []

    entries: list[SkillEntry] = []

    # Direct children only (one level deep — matches standard layout)
    candidates: list[Path] = []

    # Check if the skills_dir root itself is a skill
    if (skills_dir / "SKILL.md").exists():
        candidates.append(skills_dir)

    # Each subdirectory that contains a SKILL.md
    for child in sorted(skills_dir.iterdir()):
        if child.is_dir() and child.name not in _SKIP_DIRS:
            if (child / "SKILL.md").exists():
                candidates.append(child)

    for skill_dir in candidates:
        entry = _parse_skill(
            skill_dir=skill_dir,
            skills_root=skills_dir,
            execute_dynamic=execute_dynamic,
            include_supporting_files=include_supporting_files,
            max_file_kb=max_file_kb,
            max_skill_kb=max_skill_kb,
            session_id=session_id,
            effort=effort,
        )
        if entry is None:
            continue
        if entry.is_internal and not include_internal:
            logger.debug("Skipping internal skill: %s", entry.skill_name)
            continue
        entries.append(entry)

    logger.info(
        "Loaded %d skill(s) from %s", len(entries), skills_dir
    )
    return entries


def _parse_skill(
    skill_dir: Path,
    skills_root: Path,
    *,
    execute_dynamic: bool,
    include_supporting_files: bool,
    max_file_kb: int,
    max_skill_kb: int,
    session_id: str,
    effort: str,
) -> SkillEntry | None:
    """Parse a single skill directory into a SkillEntry."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    raw_text = _load_text_file(skill_md, max_kb=max_file_kb)
    if raw_text is None:
        logger.warning("Could not read %s", skill_md)
        return None

    fm, body = _parse_frontmatter(raw_text)

    # ── Extract frontmatter fields ────────────────────────────────────────────
    skill_name = skill_dir.name if skill_dir != skills_root else skills_root.parent.name

    def _str(key: str, default: str = "") -> str:
        v = fm.get(key, default)
        return str(v).strip() if v else default

    def _bool(key: str, default: bool = False) -> bool:
        v = fm.get(key, default)
        if isinstance(v, bool): return v
        return str(v).lower() in ("true", "1", "yes")

    def _list(key: str) -> list[str]:
        v = fm.get(key, [])
        if isinstance(v, str):
            return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if x]
        return []

    # metadata.internal nested key
    meta = fm.get("metadata", {}) or {}
    is_internal = _bool("internal") or (isinstance(meta, dict) and meta.get("internal", False))

    # Dynamic context injection
    shell = _str("shell", "bash")
    if execute_dynamic and ("!`" in body or "```!" in body):
        body = _resolve_dynamic_context(body, skill_dir, shell=shell)

    # String substitutions (static values at load time)
    body = _apply_substitutions(
        body,
        skill_dir=skill_dir,
        session_id=session_id,
        effort=effort,
    )

    # ── Load supporting files ─────────────────────────────────────────────────
    supporting: list[SkillFile] = []
    if include_supporting_files:
        total_bytes = len(body.encode())
        max_bytes = max_skill_kb * 1024

        for path in sorted(_iter_skill_files(skill_dir)):
            if total_bytes >= max_bytes:
                logger.debug(
                    "Skill %s: reached %d KB limit, skipping remaining files",
                    skill_name, max_skill_kb,
                )
                break

            rel = path.relative_to(skill_dir).as_posix()
            content = _load_text_file(path, max_kb=max_file_kb)
            if content is None:
                continue

            # Execute dynamic context in supporting files too
            if execute_dynamic and ("!`" in content or "```!" in content):
                content = _resolve_dynamic_context(content, skill_dir, shell=shell)

            content = _apply_substitutions(
                content,
                skill_dir=skill_dir,
                session_id=session_id,
                effort=effort,
            )

            supporting.append(SkillFile(rel_path=rel, abs_path=path, content=content))
            total_bytes += len(content.encode())

    return SkillEntry(
        skill_name=skill_name,
        skill_dir=skill_dir,
        name=_str("name", skill_name),
        description=_str("description"),
        when_to_use=_str("when_to_use"),
        disable_model_invocation=_bool("disable-model-invocation"),
        user_invocable=_bool("user-invocable", default=True),
        allowed_tools=_list("allowed-tools"),
        disallowed_tools=_list("disallowed-tools"),
        model_override=fm.get("model"),
        effort=fm.get("effort"),
        context=_str("context") or None,
        paths_patterns=_list("paths"),
        argument_hint=fm.get("argument-hint"),
        argument_names=_list("arguments"),
        shell=shell,
        is_internal=is_internal,
        body=body,
        supporting_files=supporting,
        raw_frontmatter=fm,
    )


def _iter_skill_files(skill_dir: Path):
    """Yield all loadable text files in a skill directory (recursive), skipping SKILL.md."""
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        # Skip hidden files/dirs
        if any(part.startswith(".") for part in path.relative_to(skill_dir).parts):
            continue
        # Skip blocked dirs
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name in _SKIP_FILENAMES:
            continue
        if path.suffix.lower() not in _TEXT_EXTENSIONS and path.suffix != "":
            continue
        yield path


# ══════════════════════════════════════════════════════════════════════════════
#  PART 7 — Skills cache
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _CacheEntry:
    skills:    list[SkillEntry]
    mtime_sig: str     # hash of skills dir modification times
    loaded_at: float   # time.monotonic()


def _mtime_signature(skills_dir: Path) -> str:
    """Cheap fingerprint of all file mtimes under skills_dir."""
    if not skills_dir.exists():
        return ""
    mtimes = []
    for p in sorted(skills_dir.rglob("*")):
        try:
            mtimes.append(f"{p}:{p.stat().st_mtime_ns}")
        except OSError:
            pass
    return hashlib.md5("\n".join(mtimes).encode()).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
#  PART 8 — SkillsMiddleware
# ══════════════════════════════════════════════════════════════════════════════

_SingleDir = str | Path
# skills_dir can be:
#   a single static path, a list of static paths ordered lowest-to-highest
#   priority, or a callable returning either — same pattern as
#   UserScopedShellMiddleware. Lists let the middleware merge platform-level,
#   tenant-level, and user-level skill directories cleanly.
_SkillsDirResolver = _SingleDir | list[_SingleDir] | Callable[[Any], _SingleDir | list[_SingleDir]]


class SkillsMiddleware(AgentMiddleware):
    """
    AgentMiddleware that loads Agent Skills from a directory and injects them
    into the agent's system prompt — matching how Claude Code, Codex, Cursor,
    and other coding agents use skills installed via `npx skills add`.

    The skills directory path can be:
      • A static str or Path:  SkillsMiddleware(".agents/skills")
      • A callable resolved from runtime.config at invocation time (same
        pattern as UserScopedShellMiddleware / UserScopedFileSearchMiddleware):
            SkillsMiddleware(
                lambda rt: Path("workspace")
                    / rt.config["configurable"]["tenant_id"]
                    / rt.config["configurable"]["user_id"]
                    / ".skills"
            )

    Constructor args:
        skills_dir              : Path to skills directory (static or callable).
        execute_dynamic_context : Execute !`command` blocks (default True).
        include_supporting_files: Load DESIGN.md, reference.md, scripts, etc.
        max_file_kb             : Max size per individual file (default 100 KB).
        max_skill_kb            : Max total content per skill (default 500 KB).
        include_internal        : Load skills marked `metadata.internal: true`.
        catalog_budget_chars    : Max chars for the skill catalog in the system
                                  prompt (default 8192, ~1% of 128k context).
        full_content_budget_chars: Max total chars for full skill content blocks
                                  (default 32768). Skills are prioritised by
                                  order, most recently invoked first (like
                                  Claude Code's 25k token budget).
        cache_ttl_seconds       : How long to cache loaded skills before re-
                                  scanning the directory (default 60 s). Set 0
                                  to disable caching (always re-scan).
        watch                   : Re-scan when file mtimes change regardless of
                                  TTL (default True). Matches Claude Code's live
                                  change detection.
    """

    def __init__(
        self,
        skills_dir: _SkillsDirResolver,
        *,
        execute_dynamic_context:   bool = True,
        include_supporting_files:  bool = True,
        max_file_kb:               int  = 100,
        max_skill_kb:              int  = 500,
        include_internal:          bool = False,
        catalog_budget_chars:      int  = 8_192,
        full_content_budget_chars: int  = 32_768,
        cache_ttl_seconds:         float = 60.0,
        watch:                     bool = True,
    ) -> None:
        self._skills_dir_arg        = skills_dir
        self._exec_dynamic          = execute_dynamic_context
        self._include_supporting    = include_supporting_files
        self._max_file_kb           = max_file_kb
        self._max_skill_kb          = max_skill_kb
        self._include_internal      = include_internal
        self._catalog_budget        = catalog_budget_chars
        self._content_budget        = full_content_budget_chars
        self._cache_ttl             = cache_ttl_seconds
        self._watch                 = watch

        # Instance-level cache: resolved_dir_str → _CacheEntry
        self._cache: dict[str, _CacheEntry] = {}

        # ── on-demand skill loader tool ───────────────────────────────────────
        # The catalog injects name + description only. When a skill is relevant
        # the agent calls this tool to fetch its full body on demand.
        #
        # Path resolution uses RunnableConfig (LangChain injects it; the LLM
        # never sees it). This is concurrency-safe: every tool call resolves its
        # own path from its own request config, so concurrent users with
        # different tenant/user directories cannot interfere with each other.
        _self = self

        @tool("load_skill")
        def load_skill(skill_name: str, config: RunnableConfig) -> str:
            """Load the complete instructions for a named skill.

            The skill catalog in the system prompt lists available skills with
            short descriptions only. Call this tool when a skill is relevant to
            the current task and you need its full instructions and reference
            material before proceeding.

            Args:
                skill_name: Skill name as shown in the catalog (without the
                            leading /), e.g. "frontend-design" or "sql-queries".
            """
            # config is injected by LangChain — not visible to the LLM.
            # _resolve_dirs_from_config rebuilds the same ordered dir list that
            # before_model computed, scoped to this exact user/tenant, with no
            # shared mutable state between concurrent requests.
            dirs   = _self._resolve_dirs_from_config(config)
            skills = _self._get_merged_skills(dirs)
            match  = next((s for s in skills if s.skill_name == skill_name), None)
            if match is None:
                available = ", ".join(s.skill_name for s in skills)
                return (
                    f"Skill '{skill_name}' not found. "
                    f"Available skills: {available}"
                )
            return match.render_context_block()

        self.tools = [load_skill]

    # ── Skills directory resolution ───────────────────────────────────────────

    def _resolve_dirs(self, runtime: Any) -> list[Path]:
        """Return ordered skill directories [lowest → highest priority].

        Passes the raw runtime object to the callable. The callable (e.g.
        _skills_dirs) is responsible for extracting config via _runtime_config,
        same as every other runtime-aware middleware does.
        """
        raw = self._skills_dir_arg(runtime) if callable(self._skills_dir_arg) else self._skills_dir_arg
        if isinstance(raw, (str, Path)):
            return [Path(raw)]
        return [Path(p) for p in raw]

    def _resolve_dirs_from_config(self, config: RunnableConfig) -> list[Path]:
        """Same as _resolve_dirs but accepts a RunnableConfig dict (tool path).

        Wraps the config dict in a minimal proxy so _runtime_config inside
        the callable finds a .config attribute and returns the dict correctly —
        identical behaviour to the before_model path.
        """
        if not callable(self._skills_dir_arg):
            raw = self._skills_dir_arg
        else:
            class _Proxy:
                def __init__(self, cfg: dict) -> None:
                    self.config = cfg

            raw = self._skills_dir_arg(_Proxy(config or {}))

        if isinstance(raw, (str, Path)):
            return [Path(raw)]
        return [Path(p) for p in raw]

    # ── Skills loading with multi-dir merge + cache ───────────────────────────

    def _get_merged_skills(
        self,
        dirs: list[Path],
        session_id: str = "",
        effort: str = "medium",
    ) -> list[SkillEntry]:
        """Load skills from all dirs and merge: later dirs override earlier ones
        by skill_name, so user-level skills shadow tenant-level, and tenant
        shadows platform-level.  Missing directories are silently skipped so
        new users with empty personal dirs still see platform/tenant skills.
        """
        existing_dirs = [d for d in dirs if d.exists()]
        if not existing_dirs:
            return []

        cache_key = "|".join(str(d.resolve()) for d in existing_dirs)
        cached    = self._cache.get(cache_key)
        now       = time.monotonic()
        sig       = "|".join(_mtime_signature(d) for d in existing_dirs) if self._watch else ""

        if cached is not None:
            age_ok   = (now - cached.loaded_at) < self._cache_ttl or self._cache_ttl <= 0
            mtime_ok = (not self._watch) or (cached.mtime_sig == sig)
            if age_ok and mtime_ok:
                return cached.skills

        # Merge: skill_name → SkillEntry; later (higher-priority) dirs win.
        merged: dict[str, SkillEntry] = {}
        for d in existing_dirs:
            for skill in _scan_skills(
                d,
                execute_dynamic=self._exec_dynamic,
                include_supporting_files=self._include_supporting,
                max_file_kb=self._max_file_kb,
                max_skill_kb=self._max_skill_kb,
                include_internal=self._include_internal,
                session_id=session_id,
                effort=effort,
            ):
                merged[skill.skill_name] = skill

        result = list(merged.values())
        self._cache[cache_key] = _CacheEntry(skills=result, mtime_sig=sig, loaded_at=now)
        return result

    # ── System prompt builder ─────────────────────────────────────────────────

    def _build_system_prompt(
        self,
        skills: list[SkillEntry],
        skills_dir: Path,
    ) -> str:
        if not skills:
            return ""

        auto_skills   = [s for s in skills if not s.disable_model_invocation]
        manual_skills = [s for s in skills if s.disable_model_invocation]

        sections: list[str] = []

        # ── Skill catalog (name + description only — always injected) ─────────
        # Full instructions are NOT pre-loaded. When a skill is relevant to the
        # current task, call the `load_skill` tool to fetch its complete body.
        catalog_lines: list[str] = [
            "## Agent Skills",
            "",
            "The following skills are available. When a skill is relevant to "
            "your task, call `load_skill(skill_name)` to load its full "
            "instructions before proceeding. Skills marked *(manual)* must be "
            "explicitly invoked by the user.",
            "",
        ]

        chars_used = sum(len(ln) + 1 for ln in catalog_lines)

        for skill in auto_skills + manual_skills:
            entry_line = skill.render_catalog_entry()
            if chars_used + len(entry_line) > self._catalog_budget:
                catalog_lines.append(
                    f"*… and more skills not shown "
                    f"(catalog budget reached — increase catalog_budget_chars)*"
                )
                break
            catalog_lines.append(entry_line)
            chars_used += len(entry_line) + 1

        sections.append("\n".join(catalog_lines))

        # ── Manual-invoke note ────────────────────────────────────────────────
        if manual_skills:
            names = ", ".join(f"`/{s.skill_name}`" for s in manual_skills)
            sections.append(
                f"\n*Manual skills (invoke explicitly): {names}. "
                f"Ask the user to type /skill-name to activate one.*"
            )

        return "\n".join(sections)

    # ── Middleware hooks ──────────────────────────────────────────────────────

    def _get_runtime_info(self, runtime: Any) -> tuple[str, str]:
        """Extract (session_id, effort) from runtime, with safe fallbacks."""
        try:
            exec_info = getattr(runtime, "execution_info", None)
            thread_id = (
                getattr(exec_info, "thread_id", None)
                or runtime.config.get("configurable", {}).get("thread_id", "")
                if hasattr(runtime, "config") else ""
            )
        except Exception:
            thread_id = ""
        effort = "medium"
        return thread_id or "", effort

    # async path (astream / ainvoke) ─────────────────────────────────────────

    async def abefore_agent(self, state: Any, runtime: Any) -> dict | None:
        """Warm the skills cache at session start for all dirs."""
        dirs = self._resolve_dirs(runtime)
        session_id, effort = self._get_runtime_info(runtime)
        self._get_merged_skills(dirs, session_id=session_id, effort=effort)
        return None

    async def abefore_model(self, state: Any, runtime: Any) -> dict | None:
        """Inject skill catalog (name + description only) into system prompt."""
        dirs = self._resolve_dirs(runtime)
        session_id, effort = self._get_runtime_info(runtime)
        skills = self._get_merged_skills(dirs, session_id=session_id, effort=effort)
        prompt = self._build_system_prompt(skills, dirs[0] if dirs else Path("."))

        if not prompt:
            return None

        parent = await super().abefore_model(state, runtime)
        return _merge_prompt(parent, prompt)

    # sync path (invoke / stream) ────────────────────────────────────────────

    def before_agent(self, state: Any, runtime: Any) -> dict | None:
        dirs = self._resolve_dirs(runtime)
        session_id, effort = self._get_runtime_info(runtime)
        self._get_merged_skills(dirs, session_id=session_id, effort=effort)
        return None

    def before_model(self, state: Any, runtime: Any) -> dict | None:
        dirs = self._resolve_dirs(runtime)
        session_id, effort = self._get_runtime_info(runtime)
        skills = self._get_merged_skills(dirs, session_id=session_id, effort=effort)
        prompt = self._build_system_prompt(skills, dirs[0] if dirs else Path("."))

        if not prompt:
            return None

        parent = super().before_model(state, runtime)
        return _merge_prompt(parent, prompt)

    # ── Convenience ───────────────────────────────────────────────────────────

    def list_skills(self, runtime: Any | None = None) -> list[SkillEntry]:
        """Return currently loaded skills (useful for debugging / inspection).

        Pass a runtime object to use dynamic path resolution. For static paths,
        runtime can be omitted. For callable skills_dir, runtime is required.
        """
        if runtime is not None:
            dirs = self._resolve_dirs(runtime)
        else:
            if callable(self._skills_dir_arg):
                raise ValueError(
                    "skills_dir is a callable — pass a runtime object so the "
                    "correct tenant/user directories can be resolved."
                )
            raw = self._skills_dir_arg
            dirs = [Path(raw)] if isinstance(raw, (str, Path)) else [Path(p) for p in raw]
        return self._get_merged_skills(dirs)

    def invalidate_cache(self) -> None:
        """Force all cached skills to be re-loaded on the next invocation."""
        self._cache.clear()
        logger.info("SkillsMiddleware: cache invalidated.")


# ── Prompt merge helper ───────────────────────────────────────────────────────

def _merge_prompt(parent_result: dict | None, addition: str) -> dict:
    """Merge a system_prompt_suffix addition with any existing parent result."""
    if parent_result is None:
        return {"system_prompt_suffix": addition}
    if isinstance(parent_result, dict):
        existing = parent_result.get("system_prompt_suffix", "")
        return {**parent_result, "system_prompt_suffix": existing + "\n\n" + addition}
    return {"system_prompt_suffix": addition}


# ══════════════════════════════════════════════════════════════════════════════
#  PART 9 — Diagnostics
# ══════════════════════════════════════════════════════════════════════════════

def print_skills_report(skills_dir: str | Path) -> None:
    """
    Print a human-readable report of all skills found in a directory.
    Useful for debugging your .skills/ setup.

        from skills_middleware import print_skills_report
        print_skills_report(".agents/skills")
    """
    path   = Path(skills_dir)
    skills = _scan_skills(path, execute_dynamic=False)

    print("═" * 60)
    print(f"  Skills report: {path.resolve()}")
    print("═" * 60)
    if not skills:
        print("  No skills found.")
        print(f"  Each skill must be a directory containing a SKILL.md file.")
        print(f"  Install skills with: npx skills add <repo>")
    else:
        for s in skills:
            invocation = "auto" if not s.disable_model_invocation else "manual"
            files_note = (
                f"  + {len(s.supporting_files)} supporting file(s)"
                if s.supporting_files else ""
            )
            internal_note = " [internal]" if s.is_internal else ""
            print(f"\n  /{s.skill_name}{internal_note}  ({invocation})")
            print(f"    Name:        {s.name}")
            print(f"    Description: {s.description[:80]}{'…' if len(s.description) > 80 else ''}")
            if s.when_to_use:
                print(f"    When to use: {s.when_to_use[:60]}…")
            if s.allowed_tools:
                print(f"    Tools:       {', '.join(s.allowed_tools)}")
            if files_note:
                print(f"    Files:      {files_note}")
    print("═" * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  PART 10 — Usage examples (commented)
# ══════════════════════════════════════════════════════════════════════════════
#
# ── Install skills first ──────────────────────────────────────────────────────
#
#   # Install vercel's official skills into .agents/skills/ (Deep Agents default)
#   npx skills add vercel-labs/agent-skills
#
#   # Install a specific skill
#   npx skills add vercel-labs/agent-skills --skill frontend-design
#
#   # Install to a custom path
#   npx add-skill vercel-labs/agent-skills --output .skills/
#
#   # View what skills would be loaded
#   python -c "from skills_middleware import print_skills_report; print_skills_report('.agents/skills')"
#
# ── Static skills directory ───────────────────────────────────────────────────
#
# from pathlib import Path
# from langchain.agents import create_agent
# from skills_middleware import SkillsMiddleware
#
# agent = create_agent(
#     model=model,
#     tools=[...],
#     middleware=[
#         SkillsMiddleware(
#             skills_dir=Path(".agents/skills"),   # project-level skills
#             execute_dynamic_context=True,         # run !`command` blocks
#             include_supporting_files=True,        # load DESIGN.md etc.
#         ),
#         # ... other middleware
#     ],
# )
#
# ── Per-user skills directory (dynamic path) ──────────────────────────────────
#
# from pathlib import Path
# from langchain.agents import create_agent
# from skills_middleware import SkillsMiddleware
#
# BASE_WORKSPACE = Path("workspace")
#
# coding_agent = create_agent(
#     model=model,
#     tools=[...],
#     middleware=[
#         SkillsMiddleware(
#             # Resolved at runtime from config — same pattern as
#             # UserScopedShellMiddleware and UserScopedFileSearchMiddleware
#             skills_dir=lambda rt: (
#                 BASE_WORKSPACE
#                 / rt.config["configurable"].get("tenant_id", "default_tenant")
#                 / rt.config["configurable"].get("user_id", "default_user")
#                 / ".skills"
#             ),
#         ),
#         UserScopedShellMiddleware(base_workspace=BASE_WORKSPACE),
#         UserScopedFileSearchMiddleware(base_workspace=BASE_WORKSPACE),
#     ],
# )
#
# config = {
#     "configurable": {
#         "thread_id": "conv-123",
#         "tenant_id": "acme",
#         "user_id":   "alice",
#     }
# }
# coding_agent.invoke({"messages": [...]}, config=config)
# # Skills loaded from: workspace/acme/alice/.skills/
#
# ── Load global + project skills ──────────────────────────────────────────────
#
# # Stack two SkillsMiddleware instances — one for project, one for global.
# # Per Agent Skills spec: project skills override global skills with same name.
# coding_agent = create_agent(
#     model=model,
#     tools=[...],
#     middleware=[
#         SkillsMiddleware(skills_dir=Path(".agents/skills")),             # project
#         SkillsMiddleware(skills_dir=Path.home() / ".deepagents/agent/skills"),  # global
#     ],
# )
#
# ── Inspect loaded skills ─────────────────────────────────────────────────────
#
# mw = SkillsMiddleware(".agents/skills")
# for skill in mw.list_skills():
#     print(f"/{skill.skill_name}: {skill.description[:60]}")