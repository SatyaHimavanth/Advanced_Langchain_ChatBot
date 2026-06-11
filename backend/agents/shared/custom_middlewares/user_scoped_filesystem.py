"""
user_scoped_filesystem.py
──────────────────────────
A BackendProtocol implementation that wraps FilesystemBackend with
per-user root_dir resolved dynamically from the LangGraph runtime config.

─────────────────────────────────────────────────────────────────────────────
The problem
─────────────────────────────────────────────────────────────────────────────
FilesystemBackend(root_dir="/app/workspace") takes a static path at init
time. With a single compiled agent serving multiple users, every user's
read_file / write_file / edit_file calls land in the same directory.

─────────────────────────────────────────────────────────────────────────────
The solution — same pattern as StoreBackend's namespace factory
─────────────────────────────────────────────────────────────────────────────
LangGraph sets context variables before executing any node or hook.
Backends can read the current invocation config at call time via
get_config() — the same mechanism used internally by StoreBackend,
StateBackend, and other built-in backends.

UserScopedFilesystemBackend calls get_config() inside every BackendProtocol
method, extracts tenant_id + user_id from config["configurable"], and
dispatches to the right FilesystemBackend(root_dir=base/tenant/user).
Per-user instances are cached (LRU) so the filesystem is not re-opened on
every call.

Effective root per call:
    base_workspace / {tenant_id} / {user_id}

─────────────────────────────────────────────────────────────────────────────
Usage — drop-in replacement inside your existing create_backend()
─────────────────────────────────────────────────────────────────────────────

    from pathlib import Path
    from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
    from user_scoped_filesystem import UserScopedFilesystemBackend, create_backend

    # Option A — use the updated create_backend factory (recommended)
    backend = create_backend(
        store=your_store,
        base_workspace=Path("workspace"),
    )

    # Option B — compose manually
    backend = CompositeBackend(
        default=StateBackend(),            # internal deepagents paths stay ephemeral
        routes={
            "/memory/":    StoreBackend(   # existing per-user memory (your pattern)
                namespace=lambda rt: (
                    "tenant", rt.config.get("configurable", {}).get("tenant_id", "default_tenant"),
                    "user",   rt.config.get("configurable", {}).get("user_id",   "default_user"),
                ),
            ),
            "/workspace/": UserScopedFilesystemBackend(
                base_workspace=Path("workspace"),
                virtual_mode=True,         # sandbox paths (strongly recommended)
            ),
        },
    )

    FilesystemMiddleware(
        backend=backend,
        system_prompt=(
            "Your workspace is at /workspace/. Use write_file, read_file, "
            "edit_file, and ls to manage files there. "
            "Use /memory/ for notes that persist across conversations."
        ),
    )

    # Invoke with config — all three routes pick up the same keys:
    config = {
        "configurable": {
            "thread_id": "conv-123",
            "tenant_id": "acme",
            "user_id":   "alice",
        }
    }
    # Disk layout:
    #   /workspace/ → workspace/acme/alice/     (UserScopedFilesystemBackend)
    #   /memory/    → StoreBackend(("tenant","acme","user","alice"))
    #   /           → StateBackend()  (ephemeral, per-thread)

─────────────────────────────────────────────────────────────────────────────
How it fits with the other per-user middlewares
─────────────────────────────────────────────────────────────────────────────
All four components now read the same two config keys at runtime:

    UserScopedShellMiddleware      → shell cwd:    workspace/tenant/user/
    UserScopedFileSearchMiddleware → search root:  workspace/tenant/user/
    UserScopedFilesystemBackend    → FS backend:   workspace/tenant/user/
    StoreBackend(namespace=...)    → store ns:     ("tenant", t, "user", u)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

logger = logging.getLogger(__name__)

# ── LangGraph get_config — try several import paths ──────────────────────────
# Backends may be called outside of graph execution (e.g. in tests); we fall
# back to {} so callers get the default tenant/user rather than crashing.

_get_config_impl: Any = None

for _mod, _fn in [
    ("langgraph.config",  "get_config"),
    ("langgraph.pregel",  "get_config"),
    ("langgraph.runtime", "get_config"),
]:
    try:
        import importlib as _il
        _get_config_impl = getattr(_il.import_module(_mod), _fn)
        break
    except (ImportError, AttributeError):
        continue

del _mod, _fn, _il  # clean up loop vars


def _get_config() -> dict:
    """Return the current LangGraph invocation config, or {} if unavailable."""
    if _get_config_impl is None:
        return {}
    try:
        result = _get_config_impl()
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  UserScopedFilesystemBackend
# ══════════════════════════════════════════════════════════════════════════════

class UserScopedFilesystemBackend(BackendProtocol):
    """
    BackendProtocol that wraps FilesystemBackend with per-user root_dir
    resolved from the LangGraph runtime config at every call.

    The per-user backend is cached (LRU, up to max_cached_users entries).
    FilesystemBackend instances are stateless disk-I/O wrappers, so caching
    is safe and concurrent-request-safe (Python's GIL + asyncio cooperativity).

    Args:
        base_workspace:   Root directory. Per-user subdirs are created here.
                          Resolved to an absolute path at init time.
        virtual_mode:     Sandbox all virtual paths under the user's root_dir,
                          blocking ../  ~ and absolute escapes.
                          Strongly recommended for multi-user deployments.
                          Default True.
        tenant_id_key:    config["configurable"] key for tenant. Default "tenant_id".
        user_id_key:      config["configurable"] key for user.   Default "user_id".
        default_tenant:   Fallback tenant when key is absent. Default "default_tenant".
        default_user:     Fallback user when key is absent.   Default "default_user".
        max_cached_users: LRU cache size (number of per-user backends). Default 256.
    """

    def __init__(
        self,
        base_workspace: Path | str,
        *,
        virtual_mode:     bool = True,
        tenant_id_key:    str  = "tenant_id",
        user_id_key:      str  = "user_id",
        default_tenant:   str  = "default_tenant",
        default_user:     str  = "default_user",
        max_cached_users: int  = 256,
    ) -> None:
        self._base          = Path(base_workspace).resolve()
        self._virtual_mode  = virtual_mode
        self._tenant_key    = tenant_id_key
        self._user_key      = user_id_key
        self._default_tenant = default_tenant
        self._default_user  = default_user
        self._max_cached    = max_cached_users

        # LRU cache: (tenant_id, user_id) → FilesystemBackend
        # dict preserves insertion order (Python 3.7+); we pop-and-reinsert on
        # hit to maintain LRU ordering, and evict the first (oldest) key when full.
        self._backends: dict[tuple[str, str], FilesystemBackend] = {}

        self._base.mkdir(parents=True, exist_ok=True)
        logger.info(
            "UserScopedFilesystemBackend: base=%s  virtual_mode=%s",
            self._base, virtual_mode,
        )

    # ── Config resolution ─────────────────────────────────────────────────────

    def _current_key(self) -> tuple[str, str]:
        """
        Extract (tenant_id, user_id) from the current LangGraph invocation
        config.  Called at the start of every BackendProtocol method so the
        right user is always selected, even when the same backend instance
        serves concurrent requests.
        """
        cfg          = _get_config()
        configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
        tenant_id    = str(configurable.get(self._tenant_key, self._default_tenant) or self._default_tenant)
        user_id      = str(configurable.get(self._user_key,   self._default_user)   or self._default_user)
        return (tenant_id, user_id)

    # ── Per-user backend cache ────────────────────────────────────────────────

    def _get_backend(self) -> FilesystemBackend:
        """
        Return the FilesystemBackend for the current user.
        Creates and caches the instance on first access; evicts the oldest
        entry (LRU) when the cache is full.
        """
        key = self._current_key()

        if key in self._backends:
            # Refresh LRU position
            backend = self._backends.pop(key)
            self._backends[key] = backend
            return backend

        # Evict oldest entry when cache is full
        if len(self._backends) >= self._max_cached:
            evicted = next(iter(self._backends))
            del self._backends[evicted]
            logger.debug(
                "UserScopedFilesystemBackend: LRU eviction for tenant=%s user=%s",
                *evicted,
            )

        tenant_id, user_id = key
        root_dir = (self._base / tenant_id / user_id).resolve()
        root_dir.mkdir(parents=True, exist_ok=True)

        backend = FilesystemBackend(
            root_dir=str(root_dir),
            virtual_mode=self._virtual_mode,
        )
        self._backends[key] = backend

        logger.info(
            "UserScopedFilesystemBackend: new backend  tenant=%s  user=%s  root=%s",
            tenant_id, user_id, root_dir,
        )
        return backend

    # ── BackendProtocol — all methods delegate to _get_backend() ─────────────
    #
    # _get_backend() calls _current_key() which calls _get_config().
    # get_config() reads LangGraph's context variable which is set for the
    # duration of the current node/hook/tool execution — safe for concurrent
    # async requests on the same event loop.

    def ls(self, path: str) -> LsResult:
        return self._get_backend().ls(path)

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit:  int = 2000,
    ) -> ReadResult:
        return self._get_backend().read(file_path, offset=offset, limit=limit)

    def grep(
        self,
        pattern: str,
        path:    str | None = None,
        glob:    str | None = None,
    ) -> GrepResult:
        return self._get_backend().grep(pattern, path, glob)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        return self._get_backend().glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._get_backend().write(file_path, content)

    def edit(
        self,
        file_path:   str,
        old_string:  str,
        new_string:  str,
        replace_all: bool = False,
    ) -> EditResult:
        return self._get_backend().edit(
            file_path, old_string, new_string, replace_all
        )

    # ── Convenience / diagnostics ─────────────────────────────────────────────

    def user_root(
        self,
        tenant_id: str | None = None,
        user_id:   str | None = None,
    ) -> Path:
        """
        Return the on-disk root directory for a given (or current) user.

            # During graph execution — resolved from config:
            root = backend.user_root()

            # Outside graph execution — pass keys explicitly:
            root = backend.user_root(tenant_id="acme", user_id="alice")
        """
        if tenant_id is None or user_id is None:
            tenant_id, user_id = self._current_key()
        return self._base / tenant_id / user_id

    def cached_users(self) -> list[tuple[str, str]]:
        """Return the (tenant_id, user_id) pairs currently in the LRU cache."""
        return list(self._backends.keys())

    def evict(self, tenant_id: str, user_id: str) -> None:
        """Remove a specific user's cached backend (forces re-creation on next call)."""
        removed = self._backends.pop((tenant_id, user_id), None)
        if removed:
            logger.info(
                "UserScopedFilesystemBackend: manually evicted tenant=%s user=%s",
                tenant_id, user_id,
            )

    def evict_all(self) -> None:
        """Clear the entire backend cache."""
        self._backends.clear()
        logger.info("UserScopedFilesystemBackend: cache cleared.")


# ══════════════════════════════════════════════════════════════════════════════
#  Updated create_backend factory
# ══════════════════════════════════════════════════════════════════════════════

def create_backend(
    *,
    store: Any = None,
    base_workspace: Path | str = Path("workspace"),
    workspace_route: str = "/workspace/",
    memory_route:    str = "/memory/",
    virtual_mode:    bool = True,
) -> Any:
    """
    Updated drop-in replacement for your existing create_backend() that adds
    per-user disk-backed file storage alongside the existing StoreBackend.

    Route layout:
        /workspace/   → UserScopedFilesystemBackend(base_workspace)
                        Real files on disk, scoped to workspace/{tenant}/{user}/
        /memory/      → StoreBackend (persistent across threads, per-user ns)
        /             → StateBackend (ephemeral in-thread scratch space)

    This extends the existing CompositeBackend pattern you already use:

        Before:  CompositeBackend(default=State, routes={"/memory/": Store})
        After:   CompositeBackend(default=State, routes={
                     "/memory/":    Store (per-user),
                     "/workspace/": UserScopedFilesystemBackend,
                 })

    Args:
        store:           LangGraph BaseStore for StoreBackend.
                         Pass InMemoryStore() for local dev; omit for
                         LangSmith Deployment (platform provisions it).
        base_workspace:  Root directory for disk-backed files. Per-user
                         subdirs are created automatically.
        workspace_route: Virtual path prefix routed to disk. Default "/workspace/".
        memory_route:    Virtual path prefix routed to StoreBackend. Default "/memory/".
        virtual_mode:    Sandbox disk paths under each user's root_dir.
                         Default True (strongly recommended).

    Returns:
        A CompositeBackend instance ready to pass to FilesystemMiddleware
        or create_agent(backend=...).
    """
    try:
        from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
        from langgraph.store.memory import InMemoryStore
    except ImportError as exc:
        raise ImportError(
            "deepagents and langgraph are required for create_backend(). "
            f"Original error: {exc}"
        ) from exc

    if store is None:
        store = InMemoryStore()

    # ── Per-user persistent memory (your existing pattern) ────────────────────
    persistent_backend = StoreBackend(
        store=store,
        namespace=lambda rt: (
            "tenant",
            (rt.config.get("configurable", {}) if hasattr(rt, "config") else {})
            .get("tenant_id", "default_tenant"),
            "user",
            (rt.config.get("configurable", {}) if hasattr(rt, "config") else {})
            .get("user_id", "default_user"),
        ),
    )

    # ── Per-user disk-backed workspace (new) ──────────────────────────────────
    filesystem_backend = UserScopedFilesystemBackend(
        base_workspace=Path(base_workspace),
        virtual_mode=virtual_mode,
    )

    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            memory_route:    persistent_backend,
            workspace_route: filesystem_backend,
        },
    )

    logger.info(
        "create_backend: CompositeBackend ready  "
        "workspace_route=%s → disk  memory_route=%s → store  / → state",
        workspace_route, memory_route,
    )
    return backend


# ══════════════════════════════════════════════════════════════════════════════
#  Full wiring example (commented)
# ══════════════════════════════════════════════════════════════════════════════
#
# from pathlib import Path
# from langchain.agents import create_agent
# from langchain.agents.middleware import HostExecutionPolicy
# from deepagents.middleware.filesystem import FilesystemMiddleware
# from langgraph.checkpoint.memory import InMemorySaver
# from langgraph.store.memory import InMemoryStore
# from user_scoped_filesystem import create_backend, UserScopedFilesystemBackend
# from user_scoped_workspace import UserScopedShellMiddleware, UserScopedFileSearchMiddleware
# from skills_middleware import SkillsMiddleware
# from mcp_prompt_middleware import MCPPromptMiddleware
#
# WORKSPACE = Path(__file__).parent / "workspace"
#
# store   = InMemoryStore()   # swap for Redis / Postgres in production
# backend = create_backend(store=store, base_workspace=WORKSPACE)
#
# coding_agent = create_agent(
#     model=model,
#     tools=[*mcp_tools, prompt_mw.prompt_tool],
#     middleware=[
#         # ── Filesystem (read / write / edit / ls on disk) ──────────────────
#         FilesystemMiddleware(
#             backend=backend,
#             system_prompt=(
#                 "Your workspace is at /workspace/. "
#                 "Use write_file, read_file, edit_file, and ls to manage files there. "
#                 "Persistent notes go in /memory/."
#             ),
#         ),
#         # ── Shell (bash/powershell, auto-detected) ─────────────────────────
#         UserScopedShellMiddleware(
#             base_workspace=WORKSPACE,
#             execution_policy=HostExecutionPolicy(),
#         ),
#         # ── File search (glob_search + grep_search, confined per user) ─────
#         UserScopedFileSearchMiddleware(base_workspace=WORKSPACE),
#         # ── Skills ────────────────────────────────────────────────────────
#         SkillsMiddleware(".agents/skills"),
#         # ── MCP prompts ───────────────────────────────────────────────────
#         MCPPromptMiddleware(mcp_client),
#     ],
#     checkpointer=InMemorySaver(),
# )
#
# # All five components read the same two config keys:
# config = {
#     "configurable": {
#         "thread_id": "conv-abc",
#         "tenant_id": "acme",
#         "user_id":   "alice",
#     }
# }
# result = await coding_agent.ainvoke({"messages": [...]}, config=config)
#
# ── On-disk layout after invocation ──────────────────────────────────────────
#
#   workspace/
#   └── acme/
#       └── alice/
#           ├── primes.py          ← written by FilesystemMiddleware (/workspace/)
#           ├── reports/
#           │   └── summary.md
#           └── .skills/           ← if per-user skills are enabled
#
# ── Virtual filesystem as seen by the agent ───────────────────────────────────
#
#   /workspace/primes.py           → workspace/acme/alice/primes.py   (disk)
#   /workspace/reports/summary.md  → workspace/acme/alice/reports/... (disk)
#   /memory/notes.md               → StoreBackend ("tenant","acme","user","alice")
#   /large_tool_results/abc.json   → StateBackend (ephemeral)
