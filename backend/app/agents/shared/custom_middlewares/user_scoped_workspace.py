"""
user_scoped_workspace.py
────────────────────────
Per-user workspace isolation for ShellToolMiddleware and FilesystemFileSearchMiddleware.

Auto-detects the best available terminal and search tooling on the host OS,
generates appropriate system prompts, and scopes all I/O to a per-user
subdirectory resolved at runtime from the LangGraph request config.

─────────────────────────────────────────────────────────────────────────────
Supported terminals
─────────────────────────────────────────────────────────────────────────────
Windows   : PowerShell Core (pwsh), Windows PowerShell (powershell),
            Git Bash, WSL bash, cmd.exe
Linux     : bash, zsh, fish, dash, sh
macOS     : zsh, bash, fish, sh

Elevated  : Linux/macOS — passwordless sudo (sudo -n)
            Windows     — detected via IsUserAnAdmin(); UAC elevation is not
                          automatable; run the server process as admin instead.

─────────────────────────────────────────────────────────────────────────────
Supported file search backends
─────────────────────────────────────────────────────────────────────────────
Auto-detected in priority order: ripgrep (rg) → Python regex fallback
Both glob_search and grep_search tool args are rewritten per-user at runtime.

─────────────────────────────────────────────────────────────────────────────
Usage
─────────────────────────────────────────────────────────────────────────────
    from pathlib import Path
    from user_scoped_workspace import UserScopedShellMiddleware, UserScopedFileSearchMiddleware

    WORKSPACE = Path(__file__).parent / "workspace"

    coding_agent = create_agent(
        model=...,
        tools=[web_search],
        middleware=[
            UserScopedShellMiddleware(base_workspace=WORKSPACE),
            UserScopedFileSearchMiddleware(base_workspace=WORKSPACE),
            FilesystemMiddleware(backend=your_backend),
        ],
    )

    # Pass tenant_id + user_id in config on every invocation:
    config = {"configurable": {"thread_id": "t1", "tenant_id": "acme", "user_id": "alice"}}
    coding_agent.invoke({"messages": [...]}, config=config)
    # Shell cwd:    workspace/acme/alice/
    # File search:  workspace/acme/alice/** only
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import (
    FilesystemFileSearchMiddleware,
    ShellToolMiddleware,
)

logger = logging.getLogger(__name__)


# ── Runtime config resolution ────────────────────────────────────────────────
# The middleware hooks receive a `runtime` object whose API has changed across
# langgraph versions. Newer `Runtime` no longer exposes `.config`. We resolve
# the invocation config defensively: prefer runtime attributes, then fall back
# to langgraph's get_config() context var (same mechanism the backends use).

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
    # 1. Direct attribute (older langgraph Runtime).
    cfg = getattr(runtime, "config", None)
    if isinstance(cfg, dict):
        return cfg
    # 2. Some versions nest it differently.
    for attr in ("_config", "configurable"):
        val = getattr(runtime, attr, None)
        if isinstance(val, dict):
            return {"configurable": val} if attr == "configurable" else val
    # 3. Context-var fallback (works inside graph execution).
    if _get_config_impl is not None:
        try:
            result = _get_config_impl()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {}



# ══════════════════════════════════════════════════════════════════════════════
#  PART 1 — Shell auto-detection
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ShellProfile:
    """
    Everything the middleware needs to know about the detected terminal.

    name          : Human-readable name  e.g. "PowerShell Core", "bash"
    shell_type    : Canonical type key used for prompt lookup
    command       : argv list passed to ShellToolMiddleware as shell_command
    os_family     : "windows" | "linux" | "darwin"
    is_elevated   : True if the shell will run with admin / root privileges
    init_commands : Extra shell commands to run right after session starts
    """
    name:          str
    shell_type:    str
    command:       list[str]
    os_family:     str
    is_elevated:   bool        = False
    init_commands: list[str]   = field(default_factory=list)


# ── Elevation detection ───────────────────────────────────────────────────────

def _is_windows_admin() -> bool:
    """True if the current process has Windows administrator privileges."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _can_passwordless_sudo(shell_bin: str) -> bool:
    """True if `sudo -n <shell> -c true` exits 0 (no password required)."""
    try:
        r = subprocess.run(
            ["sudo", "-n", shell_bin, "-c", "true"],
            capture_output=True,
            timeout=4,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── Windows path helpers ──────────────────────────────────────────────────────

def _find_git_bash() -> str | None:
    """Return the full path to Git for Windows bash.exe if installed."""
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        Path.home() / "AppData/Local/Programs/Git/bin/bash.exe",
        # Scoop
        Path.home() / "scoop/apps/git/current/bin/bash.exe",
    ]
    for p in candidates:
        if Path(p).exists():
            return str(p)
    return None


def windows_path_to_wsl(path: Path) -> str:
    """
    Convert a Windows Path to its WSL equivalent.
    e.g. C:\\workspace\\acme → /mnt/c/workspace/acme
    """
    s = path.as_posix()
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def _windows_path_to_gitbash(path: Path) -> str:
    """
    Convert a Windows Path to its Git Bash (MSYS) equivalent.
    e.g. C:\\workspace\\acme → /c/workspace/acme
    """
    s = path.as_posix()
    if len(s) >= 2 and s[1] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


# ── OS-specific shell detectors ───────────────────────────────────────────────

def _detect_windows(use_elevated: bool) -> ShellProfile:
    already_admin = _is_windows_admin()

    # 1. Git Bash — PREFERRED on Windows.
    # ShellToolMiddleware drives a persistent shell over stdin and detects
    # command completion via POSIX-style exit-code markers. Git Bash supports
    # this cleanly; PowerShell over piped stdin does NOT reliably report exit
    # codes in the expected form, which causes the startup command to hang and
    # time out. So we try bash first and only fall back to PowerShell/cmd.
    git_bash = _find_git_bash()
    if git_bash:
        return ShellProfile(
            name="Git Bash",
            shell_type="bash",
            command=[git_bash, "--noprofile", "--norc"],
            os_family="windows",
            is_elevated=already_admin,  # bash via Git for Windows still bound by Windows ACLs
            init_commands=["export PS1='\\w$ '"],
        )

    # 2. WSL bash
    if shutil.which("wsl"):
        return ShellProfile(
            name="WSL bash",
            shell_type="bash",
            command=["wsl.exe", "bash", "--noprofile", "--norc"],
            os_family="linux",   # behaves like Linux inside
            is_elevated=False,
            init_commands=[],
        )

    # 3. PowerShell Core (pwsh)
    if shutil.which("pwsh"):
        return ShellProfile(
            name="PowerShell Core",
            shell_type="powershell_core",
            # No -NonInteractive — the middleware drives the shell via stdin.
            command=["pwsh.exe", "-NoLogo", "-NoProfile"],
            os_family="windows",
            is_elevated=already_admin,
            init_commands=["$ProgressPreference = 'SilentlyContinue'"],
        )

    # 4. Windows PowerShell 5
    if shutil.which("powershell"):
        return ShellProfile(
            name="Windows PowerShell",
            shell_type="powershell",
            command=["powershell.exe", "-NoLogo", "-NoProfile"],
            os_family="windows",
            is_elevated=already_admin,
            init_commands=["$ProgressPreference = 'SilentlyContinue'"],
        )

    # 5. cmd.exe — always present on Windows, least capable
    return ShellProfile(
        name="Command Prompt",
        shell_type="cmd",
        command=["cmd.exe", "/Q"],
        os_family="windows",
        is_elevated=already_admin,
        init_commands=["@echo off"],
    )


def _detect_unix(os_family: str, use_elevated: bool) -> ShellProfile:
    # Ordered preference: bash > zsh > fish > dash > sh
    candidates = [
        ("bash",  "bash"),
        ("zsh",   "zsh"),
        ("fish",  "fish"),
        ("dash",  "dash"),
        ("sh",    "sh"),
    ]

    for bin_name, shell_type in candidates:
        resolved = shutil.which(bin_name)
        if resolved is None:
            continue

        elevated = False
        command  = [resolved, "--noprofile", "--norc"] if shell_type != "sh" else [resolved]

        if use_elevated and _can_passwordless_sudo(resolved):
            elevated = True
            command  = ["sudo", "-n"] + command

        # zsh doesn't accept --noprofile --norc; use -d -f instead
        if shell_type == "zsh":
            command = (["sudo", "-n"] if elevated else []) + [resolved, "-d", "-f"]
        # fish: no --noprofile, use -N (no config)
        if shell_type == "fish":
            command = (["sudo", "-n"] if elevated else []) + [resolved, "-N"]

        init = []
        if shell_type in ("bash", "zsh"):
            init.append("export PS1='\\w$ '")
        if shell_type == "fish":
            init.append("function fish_prompt; echo (prompt_pwd)'$ '; end")

        return ShellProfile(
            name=bin_name,
            shell_type=shell_type,
            command=command,
            os_family=os_family,
            is_elevated=elevated,
            init_commands=init,
        )

    # Should never reach here — /bin/sh exists on every POSIX system
    return ShellProfile(
        name="sh", shell_type="sh",
        command=["/bin/sh"], os_family=os_family,
    )


def detect_shell(use_elevated: bool = False) -> ShellProfile:
    """
    Auto-detect the best available shell for the current OS.

    Args:
        use_elevated: If True, attempt to detect a shell with elevated
                      privileges (passwordless sudo on Linux/macOS, existing
                      admin token on Windows).

    Returns:
        A ShellProfile describing the detected terminal.
    """
    if sys.platform == "win32":
        return _detect_windows(use_elevated)
    elif sys.platform == "darwin":
        return _detect_unix("darwin", use_elevated)
    else:
        return _detect_unix("linux", use_elevated)


# ── Per-shell tool descriptions ───────────────────────────────────────────────
# These are passed as `tool_description` to ShellToolMiddleware so the LLM
# understands the shell syntax.  The {elevated} placeholder is filled in at
# init time.  Workspace path is injected at runtime via abefore_model.

_SHELL_TOOL_DESCRIPTIONS: dict[str, str] = {

    "powershell_core": (
        "Execute commands in a persistent PowerShell Core (pwsh {version}) session "
        "on {os}.\n"
        "Syntax guide:\n"
        "  List files:    Get-ChildItem  (aliases: ls, dir, gci)\n"
        "  Change dir:    Set-Location   (alias: cd)\n"
        "  Create file:   New-Item -ItemType File -Path file.txt\n"
        "  Create dir:    New-Item -ItemType Directory  (alias: mkdir)\n"
        "  Read file:     Get-Content    (aliases: cat, type, gc)\n"
        "  Delete:        Remove-Item    (aliases: rm, del, ri)\n"
        "  Search files:  Get-ChildItem -Recurse -Filter *.py\n"
        "  Search content:Select-String -Pattern 'regex' -Path *.py\n"
        "  Run script:    .\\\\script.ps1\n"
        "  Variables:     $env:WORKSPACE  |  $PSVersionTable\n"
        "  Conditionals:  if ($x -eq 1) {{ ... }}\n"
        "  Exit code:     $LASTEXITCODE  (not $?)\n"
        "{elevated}"
    ),

    "powershell": (
        "Execute commands in a persistent Windows PowerShell 5.x session on Windows.\n"
        "Syntax guide:\n"
        "  List files:    Get-ChildItem  (aliases: ls, dir)\n"
        "  Change dir:    Set-Location   (alias: cd)\n"
        "  Create file:   New-Item -ItemType File\n"
        "  Create dir:    mkdir  or  New-Item -ItemType Directory\n"
        "  Read file:     Get-Content    (alias: cat, type)\n"
        "  Delete:        Remove-Item    (alias: rm, del)\n"
        "  Search:        Select-String -Pattern 'regex' -Path *.py\n"
        "  Run script:    .\\\\script.ps1\n"
        "  Variables:     $env:WORKSPACE\n"
        "  NOTE: Prefer New-Item over touch (touch is not built-in).\n"
        "{elevated}"
    ),

    "bash": (
        "Execute commands in a persistent bash session on {os}.\n"
        "Syntax guide:\n"
        "  List files:    ls -la  |  ls -lh --sort=time\n"
        "  Change dir:    cd path\n"
        "  Create file:   touch file  |  cat > file << 'EOF'\\ncontent\\nEOF\n"
        "  Create dir:    mkdir -p path\n"
        "  Read file:     cat file  |  head -n 20 file\n"
        "  Delete:        rm -rf path\n"
        "  Find files:    find . -name '*.py' -type f\n"
        "  Search:        grep -rn 'pattern' .  |  grep -l 'text' *.py\n"
        "  Run script:    bash script.sh  |  chmod +x s.sh && ./s.sh\n"
        "  Variables:     $WORKSPACE\n"
        "  Pipes:         cmd1 | cmd2 && cmd3 || fallback\n"
        "  Heredoc:       cat > file.py << 'EOF'\\ncode\\nEOF\n"
        "{elevated}"
    ),

    "zsh": (
        "Execute commands in a persistent zsh session on {os}.\n"
        "Same as bash with extras:\n"
        "  Extended glob: ls **/*.py  |  ls **/*.py(om)  (sort by mtime)\n"
        "  Setopt:        setopt EXTENDED_GLOB\n"
        "  Variables:     $WORKSPACE\n"
        "  Otherwise use standard bash syntax — all the same commands apply.\n"
        "{elevated}"
    ),

    "fish": (
        "Execute commands in a persistent fish shell session on {os}.\n"
        "IMPORTANT: fish syntax differs from bash:\n"
        "  Variables:     set VARNAME value          (NOT export VAR=val)\n"
        "  Environment:   set -x VARNAME value       (NOT export)\n"
        "  Conditions:    if test -f file; ...; end  (NOT if [ -f file ]; then)\n"
        "  Loops:         for f in *.py; echo $f; end\n"
        "  Workspace:     $WORKSPACE\n"
        "  Standard ops:  ls, cd, mkdir, rm, cat, grep — same as bash\n"
        "{elevated}"
    ),

    "cmd": (
        "Execute commands in a persistent Windows Command Prompt (cmd.exe) session.\n"
        "Syntax guide:\n"
        "  List files:    dir /b  |  dir /s /b *.py\n"
        "  Change dir:    cd path\n"
        "  Create file:   echo content > file.txt  |  type nul > file.txt\n"
        "  Create dir:    mkdir path\n"
        "  Read file:     type file.txt\n"
        "  Delete:        del file.txt  |  rmdir /s /q dir\n"
        "  Find files:    dir /s /b *.py\n"
        "  Search:        findstr /s /i /n \"pattern\" *.py\n"
        "  Variables:     %WORKSPACE%  (set via: set WORKSPACE=path)\n"
        "  NOTE: cmd has very limited scripting. For complex tasks ask if\n"
        "        PowerShell is available and switch to it.\n"
        "{elevated}"
    ),

    "sh": (
        "Execute commands in a POSIX sh session on {os}.\n"
        "Use POSIX-compatible syntax only — no bash extensions:\n"
        "  No arrays, no [[ ]], no process substitution <()\n"
        "  Conditions:    if [ -f file ]; then ...; fi\n"
        "  Loops:         for f in *.py; do echo $f; done\n"
        "  Variables:     $WORKSPACE\n"
        "  Standard ops:  ls, cd, mkdir, rm, cat, find, grep\n"
        "{elevated}"
    ),
}

_ELEVATION_SUFFIX = {
    True:  "\nPrivileges: ELEVATED (admin / root) — use with caution.",
    False: "\nPrivileges: standard user — sudo/RunAs not available.",
}


def _build_tool_description(profile: ShellProfile) -> str:
    template = _SHELL_TOOL_DESCRIPTIONS.get(profile.shell_type, _SHELL_TOOL_DESCRIPTIONS["sh"])
    os_label = {"windows": "Windows", "darwin": "macOS", "linux": "Linux"}.get(
        profile.os_family, profile.os_family
    )

    # Try to get PowerShell version for the description
    version = ""
    if profile.shell_type in ("powershell_core", "powershell"):
        try:
            r = subprocess.run(
                profile.command + ["-Command", "$PSVersionTable.PSVersion.ToString()"],
                capture_output=True, text=True, timeout=5,
            )
            version = r.stdout.strip() or ""
        except Exception:
            pass

    return template.format(
        os=os_label,
        elevated=_ELEVATION_SUFFIX[profile.is_elevated],
        version=version,
    ).strip()


# ══════════════════════════════════════════════════════════════════════════════
#  PART 2 — UserScopedShellMiddleware
# ══════════════════════════════════════════════════════════════════════════════

# Timeout (seconds) for the internal `cd` we run when scoping the shell session.
_CD_TIMEOUT = 15.0


def _exec_session_sync(exec_fn: Any, cmd: str) -> Any:
    """
    Call a ShellSession.execute()-style function, supplying the `timeout`
    keyword only if the installed version requires/accepts it. Tolerates both
    old (no timeout) and new (timeout required) signatures.
    """
    try:
        return exec_fn(cmd, timeout=_CD_TIMEOUT)
    except TypeError:
        # Older signature without a timeout parameter.
        return exec_fn(cmd)


async def _exec_session(exec_fn: Any, cmd: str, *, is_async: bool) -> Any:
    """Async-aware variant of _exec_session_sync (awaits async execute fns)."""
    if is_async:
        try:
            return await exec_fn(cmd, timeout=_CD_TIMEOUT)
        except TypeError:
            return await exec_fn(cmd)
    return _exec_session_sync(exec_fn, cmd)


def workspace_for(config: dict, base: Path) -> Path:
    """
    Derive a per-thread workspace path from a LangGraph request config dict.
    Layout: base / tenant_id / user_id / thread_id
    Creates the directory tree if it does not exist.
    """
    c         = config.get("configurable", {})
    tenant_id = c.get("tenant_id", "default_tenant")
    user_id   = c.get("user_id",   "default_user")
    thread_id = c.get("thread_id", "default_thread")
    workspace = base / tenant_id / user_id / thread_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


class UserScopedShellMiddleware(ShellToolMiddleware):
    """
    ShellToolMiddleware with:
      1. Auto-detection of the best available terminal on the host OS.
      2. Per-user workspace isolation resolved from runtime.config at invocation
         start (same config keys as your CompositeBackend namespace lambda).
      3. Shell-specific tool_description injected so the LLM knows what
         syntax to use.
      4. Per-user workspace path injected into the system prompt via
         abefore_model so the LLM knows where to operate.

    Config keys consumed from runtime.config["configurable"]:
        tenant_id : str  (default: "default_tenant")
        user_id   : str  (default: "default_user")

    Args:
        base_workspace  : Root directory under which per-user subdirs are created.
        shell_profile   : Override auto-detected ShellProfile (useful for tests
                          or forcing a specific terminal type).
        use_elevated    : Try to detect an elevated shell (sudo / admin).
        require_elevated: Raise at init time if elevation is unavailable.
        **kwargs        : Forwarded to ShellToolMiddleware (e.g. execution_policy,
                          startup_commands, redaction_rules, env).
    """

    def __init__(
        self,
        base_workspace: Path,
        *,
        shell_profile:    ShellProfile | None = None,
        use_elevated:     bool = False,
        require_elevated: bool = False,
        **kwargs: Any,
    ) -> None:
        self._base_workspace = Path(base_workspace)
        self._base_workspace.mkdir(parents=True, exist_ok=True)

        # Detect shell
        self._profile = shell_profile or detect_shell(use_elevated=use_elevated)

        if require_elevated and not self._profile.is_elevated:
            raise RuntimeError(
                f"UserScopedShellMiddleware: elevation required but not available "
                f"(detected: {self._profile.name}). "
                "On Linux/macOS configure passwordless sudo; "
                "on Windows run the server process as Administrator."
            )

        logger.info(
            "UserScopedShellMiddleware: using %s (elevated=%s)",
            self._profile.name, self._profile.is_elevated,
        )

        # Build the tool_description the LLM will see in its tool list.
        # The workspace path is injected per-user via abefore_model.
        tool_description = kwargs.pop("tool_description", None) or _build_tool_description(self._profile)

        # Merge user-supplied startup_commands with shell init commands and a
        # PATH export so the agent's shell can find the SAME Python the backend
        # runs under (Git Bash / a non-login shell does not inherit the Windows
        # Python PATH, so `python` would otherwise be "command not found").
        extra_startup = list(kwargs.pop("startup_commands", None) or [])
        path_startup  = self._python_path_startup()
        full_startup   = list(self._profile.init_commands) + path_startup + extra_startup

        # Bake the backend's Python dir into the shell subprocess environment so
        # `python` stays resolvable even after a session RESTART (e.g. when a
        # command times out — restart() re-spawns the process but does NOT
        # re-run startup commands, so a PATH set only via startup would be lost).
        env = self._build_env(dict(kwargs.pop("env", None) or {}))

        super().__init__(
            workspace_root=self._base_workspace,      # actual cd is done post-start
            shell_command=self._profile.command,
            tool_description=tool_description,
            startup_commands=full_startup or None,
            env=env,
            **kwargs,
        )

    def _build_env(self, extra_env: dict) -> dict:
        """
        Full environment for the shell subprocess: a copy of the parent process
        environment with the backend's Python directory prepended to PATH.
        Passing ``env`` to ShellToolMiddleware REPLACES the environment, so we
        must start from os.environ to keep system tools (git, bash, etc.) working.
        """
        import os

        env = dict(os.environ)
        py_dir = str(Path(sys.executable).parent)
        sep = ";" if sys.platform == "win32" else ":"
        # Windows env var names are case-insensitive; the key is conventionally "Path".
        path_key = "PATH"
        for k in env:
            if k.upper() == "PATH":
                path_key = k
                break
        env[path_key] = py_dir + sep + env.get(path_key, "")
        env.update(extra_env)
        return env

    # ── Python availability ────────────────────────────────────────────────────

    def _python_path_startup(self) -> list[str]:
        """
        Startup command(s) that prepend the backend's Python directory to PATH
        so `python` (and, where possible, `python3`) resolve inside the shell.
        """
        py_dir = Path(sys.executable).parent
        st = self._profile.shell_type

        if st in ("bash", "zsh", "fish", "sh"):
            # Git Bash / WSL style path. Git Bash uses /c/...; WSL uses /mnt/c/...
            if self._profile.os_family == "linux" and sys.platform == "win32":
                posix_dir = windows_path_to_wsl(py_dir)         # WSL: /mnt/c/...
            elif sys.platform == "win32":
                posix_dir = _windows_path_to_gitbash(py_dir)    # Git Bash: /c/...
            else:
                posix_dir = py_dir.as_posix()
            if st == "fish":
                return [
                    f'set -x PATH "{posix_dir}" $PATH',
                    'function python3; python $argv; end',
                ]
            # On Windows venvs only python.exe exists (no python3.exe), so add a
            # python3 shim that forwards to python for agents that prefer it.
            return [
                f'export PATH="{posix_dir}:$PATH"',
                'python3() { python "$@"; }',
            ]

        if st in ("powershell_core", "powershell"):
            return [f'$env:PATH = "{py_dir};" + $env:PATH']

        if st == "cmd":
            return [f'set PATH={py_dir};%PATH%']

        return []

    # ── Workspace resolver ────────────────────────────────────────────────────

    def _user_workspace(self, config: dict) -> Path:
        return workspace_for(config, self._base_workspace)

    def _cd_cmd(self, workspace: Path) -> str:
        """Shell-appropriate cd + env-var command."""
        p = workspace.as_posix()
        wsl_used = (
            self._profile.shell_type == "bash"
            and self._profile.os_family == "linux"
            and sys.platform == "win32"
        )
        if wsl_used:
            p = windows_path_to_wsl(workspace)

        if self._profile.shell_type in ("powershell_core", "powershell"):
            return f'Set-Location -Path "{workspace}"; $env:WORKSPACE = "{workspace}"'
        if self._profile.shell_type == "cmd":
            return f'cd /d "{workspace}" && set WORKSPACE={workspace}'
        if self._profile.shell_type == "fish":
            return f"mkdir -p '{p}'; cd '{p}'; set -x WORKSPACE '{p}'"
        # bash / zsh / sh
        return f"mkdir -p '{p}' && cd '{p}' && export WORKSPACE='{p}'"

    @staticmethod
    def _extract_session(result: dict | None) -> Any | None:
        """Unwrap shell_session_resources, handling UntrackedValue wrapper."""
        if not result:
            return None
        resources = result.get("shell_session_resources")
        if resources is None:
            return None
        actual = getattr(resources, "value", resources)
        return getattr(actual, "session", None)

    # ── Workspace prompt injection ────────────────────────────────────────────

    def _workspace_prompt(self, workspace: Path, config: dict) -> str:
        """One-line context note injected into the system prompt before each model call."""
        p = workspace.as_posix()
        wsl_used = (
            self._profile.shell_type == "bash"
            and self._profile.os_family == "linux"
            and sys.platform == "win32"
        )
        if wsl_used:
            display = windows_path_to_wsl(workspace)              # /mnt/c/...
        elif self._profile.shell_type in ("bash", "zsh", "sh", "fish") and sys.platform == "win32":
            display = _windows_path_to_gitbash(workspace)         # /c/...
        else:
            display = p

        if self._profile.shell_type in ("powershell_core", "powershell"):
            env_ref = f"$env:WORKSPACE = \"{display}\""
        elif self._profile.shell_type == "cmd":
            env_ref = f"%WORKSPACE% = {display}"
        elif self._profile.shell_type == "fish":
            env_ref = f"$WORKSPACE = {display}"
        else:
            env_ref = f"$WORKSPACE = {display}"

        return (
            f"\nShell workspace: your shell starts in your working directory "
            f"`{display}` ({env_ref}), which is the SAME directory the "
            f"filesystem tools use. Shell: {self._profile.name}.\n"
            "Use plain relative paths: e.g. `python prime.py` to run a file you "
            "created with write_file('prime.py', ...). Do NOT prefix shell paths "
            "with /workspace/."
        )

    # ── async path (astream / ainvoke) ────────────────────────────────────────

    async def abefore_agent(self, state: Any, runtime: Any) -> dict | None:
        config = _runtime_config(runtime)
        user_workspace = self._user_workspace(config)
        logger.debug("UserScopedShellMiddleware: scoping to %s", user_workspace)

        result = await super().abefore_agent(state, runtime)

        session = self._extract_session(result)
        if session is not None:
            try:
                cmd   = self._cd_cmd(user_workspace)
                aexec = getattr(session, "aexecute", None)
                if aexec is not None:
                    await _exec_session(aexec, cmd, is_async=True)
                else:
                    await _exec_session(session.execute, cmd, is_async=False)
                logger.info("UserScopedShellMiddleware: shell at %s", user_workspace)
            except Exception:
                logger.exception("UserScopedShellMiddleware: cd to %s failed", user_workspace)
        return result

    async def abefore_model(self, state: Any, runtime: Any) -> dict | None:
        config         = _runtime_config(runtime)
        user_workspace = self._user_workspace(config)
        parent_result  = await super().abefore_model(state, runtime)
        hint           = self._workspace_prompt(user_workspace, config)
        if parent_result is None:
            return {"system_prompt_suffix": hint}
        if isinstance(parent_result, dict):
            existing = parent_result.get("system_prompt_suffix", "")
            return {**parent_result, "system_prompt_suffix": existing + hint}
        return parent_result

    # ── sync path (invoke / stream) ───────────────────────────────────────────

    def before_agent(self, state: Any, runtime: Any) -> dict | None:
        config = _runtime_config(runtime)
        user_workspace = self._user_workspace(config)
        logger.debug("UserScopedShellMiddleware (sync): scoping to %s", user_workspace)

        result = super().before_agent(state, runtime)

        session = self._extract_session(result)
        if session is not None:
            try:
                _exec_session_sync(session.execute, self._cd_cmd(user_workspace))
                logger.info("UserScopedShellMiddleware (sync): shell at %s", user_workspace)
            except Exception:
                logger.exception("UserScopedShellMiddleware (sync): cd to %s failed", user_workspace)
        return result

    def before_model(self, state: Any, runtime: Any) -> dict | None:
        config         = _runtime_config(runtime)
        user_workspace = self._user_workspace(config)
        parent_result  = super().before_model(state, runtime)
        hint           = self._workspace_prompt(user_workspace, config)
        if parent_result is None:
            return {"system_prompt_suffix": hint}
        if isinstance(parent_result, dict):
            existing = parent_result.get("system_prompt_suffix", "")
            return {**parent_result, "system_prompt_suffix": existing + hint}
        return parent_result

    # ── Command rewriting ──────────────────────────────────────────────────────
    # The model frequently refers to files via the FILESYSTEM-tool virtual path
    # `/workspace/...`. In the shell that is a literal absolute path (Git Bash
    # maps `/workspace` → C:\Program Files\Git\workspace), so commands fail.
    # We transparently rewrite `/workspace/` (and `/workspace`) in the shell
    # command to the real per-thread working directory, so `python
    # /workspace/prime.py` and `python prime.py` both work.

    def _shell_dir(self, workspace: Path) -> str:
        """The workspace path as the shell expects to see it."""
        if self._profile.os_family == "linux" and sys.platform == "win32":
            return windows_path_to_wsl(workspace)
        if self._profile.shell_type in ("bash", "zsh", "sh", "fish") and sys.platform == "win32":
            return _windows_path_to_gitbash(workspace)
        if self._profile.shell_type in ("powershell_core", "powershell", "cmd"):
            return str(workspace)
        return workspace.as_posix()

    def _rewrite_command(self, command: str, config: dict) -> str:
        if not command or "/workspace" not in command:
            return command
        ws_dir = self._shell_dir(self._user_workspace(config)).rstrip("/")
        # `/workspace/foo` → `<ws_dir>/foo`; bare `/workspace` → `<ws_dir>`.
        out = command.replace("/workspace/", f"{ws_dir}/")
        out = re.sub(r"/workspace(?![\w/])", ws_dir, out)
        return out

    def _scope_shell_request(self, request: Any) -> Any:
        tool_call = getattr(request, "tool_call", None)
        if tool_call is None:
            return request
        args = tool_call.get("args") if isinstance(tool_call, dict) else getattr(tool_call, "args", None)
        if not isinstance(args, dict) or "command" not in args:
            return request
        config = _runtime_config(getattr(request, "runtime", None))
        new_cmd = self._rewrite_command(args.get("command", ""), config)
        if new_cmd == args.get("command"):
            return request
        new_args = {**args, "command": new_cmd}
        new_tool_call = (
            {**tool_call, "args": new_args}
            if isinstance(tool_call, dict)
            else {**dict(tool_call), "args": new_args}
        )
        override = getattr(request, "override", None)
        if callable(override):
            return override(tool_call=new_tool_call)
        try:
            request.tool_call = new_tool_call
        except Exception:
            pass
        return request

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        return await handler(self._scope_shell_request(request))

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        return handler(self._scope_shell_request(request))

    # ── convenience ───────────────────────────────────────────────────────────

    @property
    def detected_profile(self) -> ShellProfile:
        """The ShellProfile that was auto-detected (or explicitly supplied)."""
        return self._profile


# ══════════════════════════════════════════════════════════════════════════════
#  PART 3 — File search auto-detection
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FileSearchProfile:
    """Describes the search tooling available on the host."""
    has_ripgrep:  bool
    ripgrep_path: str | None
    os_family:    str


def detect_file_search() -> FileSearchProfile:
    """
    Auto-detect available file search backends.
    Checks for ripgrep (rg) first; falls back to Python regex.
    """
    os_family = (
        "windows" if sys.platform == "win32"
        else "darwin" if sys.platform == "darwin"
        else "linux"
    )
    rg_path = shutil.which("rg")
    return FileSearchProfile(
        has_ripgrep=rg_path is not None,
        ripgrep_path=rg_path,
        os_family=os_family,
    )


def _build_search_tool_description(profile: FileSearchProfile, base_workspace: Path) -> str:
    backend = (
        f"ripgrep ({profile.ripgrep_path})"
        if profile.has_ripgrep
        else "Python regex (ripgrep not found)"
    )
    return (
        f"Search filesystem files using glob and grep tools.\n"
        f"Backend: {backend}.\n"
        f"The search root is scoped to your workspace — you only see your own files.\n"
        f"Tools available:\n"
        f"  glob_search(pattern, path)  — find files by name pattern (e.g. **/*.py)\n"
        f"  grep_search(pattern, path, include, output_mode) — search file content\n"
        f"Always use path='/' to search your entire workspace or a subdirectory path\n"
        f"like path='/src/' to narrow the search."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PART 4 — UserScopedFileSearchMiddleware
# ══════════════════════════════════════════════════════════════════════════════

# Exact tool names added by FilesystemFileSearchMiddleware (confirmed from docs)
_SEARCH_TOOL_NAMES = frozenset({"glob_search", "grep_search"})


class UserScopedFileSearchMiddleware(FilesystemFileSearchMiddleware):
    """
    FilesystemFileSearchMiddleware with:
      1. Auto-detection of ripgrep availability.
      2. Per-user workspace isolation: glob_search and grep_search tool calls
         are intercepted and their 'path' argument is rewritten to stay within
         /{tenant_id}/{user_id}/ before execution.
      3. Path-traversal attack prevention.
      4. Per-user workspace path injected into the system prompt via
         abefore_model.

    Both the glob tool and grep tool are scoped transparently — the LLM never
    needs to know the full absolute base path; '/' always means "my workspace".

    Config keys consumed from runtime.config["configurable"]:
        tenant_id : str  (default: "default_tenant")
        user_id   : str  (default: "default_user")
    """

    def __init__(
        self,
        base_workspace: Path,
        *,
        search_profile:  FileSearchProfile | None = None,
        max_file_size_mb: int = 10,
    ) -> None:
        self._base_workspace   = Path(base_workspace)
        self._search_profile   = search_profile or detect_file_search()
        self._base_workspace.mkdir(parents=True, exist_ok=True)

        logger.info(
            "UserScopedFileSearchMiddleware: ripgrep=%s (%s)",
            self._search_profile.has_ripgrep,
            self._search_profile.ripgrep_path or "not found",
        )

        super().__init__(
            root_path=str(self._base_workspace),
            use_ripgrep=self._search_profile.has_ripgrep,
            max_file_size_mb=max_file_size_mb,
        )

    # ── Path rewriting ────────────────────────────────────────────────────────

    def _user_prefix(self, config: dict) -> str:
        """Return '/{tenant_id}/{user_id}/{thread_id}' and ensure the directory exists."""
        c         = config.get("configurable", {})
        tenant_id = c.get("tenant_id", "default_tenant")
        user_id   = c.get("user_id",   "default_user")
        thread_id = c.get("thread_id", "default_thread")
        (self._base_workspace / tenant_id / user_id / thread_id).mkdir(parents=True, exist_ok=True)
        return f"/{tenant_id}/{user_id}/{thread_id}"

    def _rewrite_path_arg(self, args: dict, config: dict) -> dict:
        """
        Rewrite (or inject) the 'path' argument so it stays within the user's
        workspace.  Blocks path-traversal attacks silently.

        Examples (tenant=acme, user=alice, base=/app/workspace):
          path absent / None → '/acme/alice/'
          path='/'           → '/acme/alice/'
          path='/reports'    → '/acme/alice/reports/'
          path='../../bob'   → '/acme/alice/'  ← traversal blocked
        """
        prefix   = self._user_prefix(config)
        original = args.get("path")

        if original is None:
            return {**args, "path": prefix + "/"}

        clean     = str(original).lstrip("/")
        candidate = f"{prefix}/{clean}".rstrip("/") + "/"

        abs_candidate = (self._base_workspace / candidate.lstrip("/")).resolve()
        abs_user_ws   = (self._base_workspace / prefix.lstrip("/")).resolve()

        if not str(abs_candidate).startswith(str(abs_user_ws)):
            logger.warning(
                "UserScopedFileSearchMiddleware: path traversal blocked. "
                "Requested %r → %s is outside %s. Falling back to workspace root.",
                original, abs_candidate, abs_user_ws,
            )
            candidate = prefix + "/"

        return {**args, "path": candidate}

    def _rewrite_tool_call(self, tool_call: Any, config: dict) -> Any:
        """Return a copy of tool_call with the path argument scoped to the user."""
        if isinstance(tool_call, dict):
            name = tool_call.get("name", "")
            args = dict(tool_call.get("args") or {})
        else:
            name = getattr(tool_call, "name", "")
            args = dict(getattr(tool_call, "args", None) or {})

        # Only rewrite calls to the two known search tools
        if name not in _SEARCH_TOOL_NAMES:
            return tool_call

        scoped = self._rewrite_path_arg(args, config)

        if isinstance(tool_call, dict):
            return {**tool_call, "args": scoped}
        for method in ("model_copy", "copy"):
            fn = getattr(tool_call, method, None)
            if fn is not None:
                return fn(update={"args": scoped})
        logger.warning(
            "UserScopedFileSearchMiddleware: cannot rewrite ToolCall of type %s; "
            "isolation not applied for this call.",
            type(tool_call).__name__,
        )
        return tool_call

    # ── Workspace prompt injection ────────────────────────────────────────────

    def _search_prompt(self, config: dict) -> str:
        prefix    = self._user_prefix(config)
        backend   = "ripgrep" if self._search_profile.has_ripgrep else "Python regex"
        return (
            f"\nFile search workspace: your root is `{prefix}/` "
            f"(search backend: {backend}). "
            f"Always pass path=\"{prefix}/\" or a subdirectory when calling "
            "glob_search or grep_search — path=\"/\" will be automatically "
            "scoped to your workspace."
        )

    # ── async path ────────────────────────────────────────────────────────────

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        new_request = self._scope_request(request)
        return await handler(new_request)

    async def abefore_model(self, state: Any, runtime: Any) -> dict | None:
        parent_result = await super().abefore_model(state, runtime)
        hint          = self._search_prompt(_runtime_config(runtime))
        if parent_result is None:
            return {"system_prompt_suffix": hint}
        if isinstance(parent_result, dict):
            existing = parent_result.get("system_prompt_suffix", "")
            return {**parent_result, "system_prompt_suffix": existing + hint}
        return parent_result

    # ── sync path ─────────────────────────────────────────────────────────────

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        new_request = self._scope_request(request)
        return handler(new_request)

    def before_model(self, state: Any, runtime: Any) -> dict | None:
        parent_result = super().before_model(state, runtime)
        hint          = self._search_prompt(_runtime_config(runtime))
        if parent_result is None:
            return {"system_prompt_suffix": hint}
        if isinstance(parent_result, dict):
            existing = parent_result.get("system_prompt_suffix", "")
            return {**parent_result, "system_prompt_suffix": existing + hint}
        return parent_result

    # ── request scoping helper ────────────────────────────────────────────────

    def _scope_request(self, request: Any) -> Any:
        """
        Return a ToolCallRequest with the search tool's `path` argument rewritten
        to stay inside the user's workspace. Non-search tools pass through
        unchanged. Compatible with the (request, handler) middleware API.
        """
        tool_call = getattr(request, "tool_call", None)
        if tool_call is None:
            return request

        # Resolve config from the request's runtime (falls back to get_config()).
        config = _runtime_config(getattr(request, "runtime", None))
        rewritten = self._rewrite_tool_call(tool_call, config)
        if rewritten is tool_call:
            return request

        override = getattr(request, "override", None)
        if callable(override):
            return override(tool_call=rewritten)
        # Fallback: best-effort mutate.
        try:
            request.tool_call = rewritten
        except Exception:
            logger.warning(
                "UserScopedFileSearchMiddleware: could not apply scoped tool_call; "
                "isolation not enforced for this call."
            )
        return request

    # ── convenience ───────────────────────────────────────────────────────────

    @property
    def detected_profile(self) -> FileSearchProfile:
        """The FileSearchProfile that was auto-detected (or explicitly supplied)."""
        return self._search_profile


# ══════════════════════════════════════════════════════════════════════════════
#  PART 5 — Diagnostics / introspection
# ══════════════════════════════════════════════════════════════════════════════

def print_detection_report(base_workspace: Path | None = None) -> None:
    """
    Print a human-readable detection report — useful for debugging your
    deployment environment.

        from user_scoped_workspace import print_detection_report
        from pathlib import Path
        print_detection_report(Path("workspace"))
    """
    shell   = detect_shell(use_elevated=False)
    elev    = detect_shell(use_elevated=True)
    search  = detect_file_search()

    print("═" * 60)
    print("  UserScopedWorkspace — detection report")
    print("═" * 60)
    print(f"  OS              : {platform.system()} {platform.release()} ({sys.platform})")
    print()
    print(f"  Best shell      : {shell.name}")
    print(f"  Command         : {' '.join(shell.command)}")
    print(f"  Shell type      : {shell.shell_type}")
    print(f"  Elevated shell  : {elev.name} (elevated={elev.is_elevated})")
    print()
    print(f"  ripgrep         : {'✓  ' + search.ripgrep_path if search.has_ripgrep else '✗  not found'}")
    if base_workspace:
        print(f"  Base workspace  : {base_workspace.resolve()}")
    print("═" * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  PART 6 — Full wiring example (commented)
# ══════════════════════════════════════════════════════════════════════════════
#
# from pathlib import Path
# from langchain.agents import create_agent
# from langchain.agents.middleware import HostExecutionPolicy
# from deepagents.middleware.filesystem import FilesystemMiddleware
# from user_scoped_workspace import (
#     UserScopedShellMiddleware,
#     UserScopedFileSearchMiddleware,
#     print_detection_report,
# )
# from agents.backends import create_backend
#
# WORKSPACE = Path(__file__).parent / "workspace"
#
# # Optionally run this at startup to verify your environment:
# print_detection_report(WORKSPACE)
#
# backend = create_backend(store=your_store)  # your existing per-user backend
#
# coding_agent = create_agent(
#     model=model,
#     tools=[web_search],
#     middleware=[
#         # Auto-detects terminal; scopes to workspace/{tenant}/{user}/ at runtime
#         UserScopedShellMiddleware(
#             base_workspace=WORKSPACE,
#             use_elevated=False,      # set True if you need sudo/admin
#             execution_policy=HostExecutionPolicy(),
#         ),
#         # Auto-detects ripgrep; scopes glob_search + grep_search per-user
#         UserScopedFileSearchMiddleware(base_workspace=WORKSPACE),
#         # Already scoped via CompositeBackend namespace lambda
#         FilesystemMiddleware(backend=backend, system_prompt="Use write_file for scratch only."),
#     ],
#     checkpointer=InMemorySaver(),
# )
#
# # All three middlewares read the same config keys:
# config = {
#     "configurable": {
#         "thread_id": "conv-123",
#         "tenant_id": "acme",
#         "user_id":   "alice",
#     }
# }
# coding_agent.invoke({"messages": [...]}, config=config)
# # Shell cwd:       workspace/acme/alice/
# # File search:     workspace/acme/alice/**  only
# # Virtual FS:      StoreBackend(namespace=("tenant","acme","user","alice"))
