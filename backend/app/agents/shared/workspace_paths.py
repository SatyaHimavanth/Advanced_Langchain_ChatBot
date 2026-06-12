"""
workspace_paths.py
──────────────────
Single source of truth for resolving the per-thread workspace directory from
the current LangGraph invocation config.

Layout (must match UserScopedFilesystemBackend / UserScopedShellMiddleware /
UserScopedFileSearchMiddleware):

    WORKSPACE_ROOT / {tenant_id} / {user_id} / {thread_id}

Tools (e.g. delete_file) call ``current_thread_dir()`` to operate on exactly
the same directory the filesystem/shell middlewares use for the active request.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from app.settings import settings

WORKSPACE_ROOT = Path(settings.WORKSPACE_ROOT).resolve()

DEFAULT_TENANT = "default_tenant"
DEFAULT_USER = "default_user"
DEFAULT_THREAD = "default_thread"

# ── LangGraph get_config — try several import paths (matches the backends). ──
_get_config_impl: Any = None
for _mod, _fn in [
    ("langgraph.config", "get_config"),
    ("langgraph.pregel", "get_config"),
    ("langgraph.runtime", "get_config"),
]:
    try:
        _get_config_impl = getattr(importlib.import_module(_mod), _fn)
        break
    except (ImportError, AttributeError):
        continue


def _get_config() -> dict:
    if _get_config_impl is None:
        return {}
    try:
        result = _get_config_impl()
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def current_scope() -> tuple[str, str, str]:
    """Return (tenant_id, user_id, thread_id) for the active invocation."""
    cfg = _get_config()
    c = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    tenant_id = str(c.get("tenant_id") or DEFAULT_TENANT)
    user_id = str(c.get("user_id") or DEFAULT_USER)
    thread_id = str(c.get("thread_id") or DEFAULT_THREAD)
    return tenant_id, user_id, thread_id


def current_thread_dir(create: bool = True) -> Path:
    """Return WORKSPACE_ROOT/tenant/user/thread for the active invocation."""
    tenant_id, user_id, thread_id = current_scope()
    d = (WORKSPACE_ROOT / tenant_id / user_id / thread_id).resolve()
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_in_thread(rel_path: str) -> Path:
    """
    Resolve a user/agent-supplied path inside the current thread directory,
    blocking traversal escapes. Accepts virtual paths like ``/workspace/foo.py``
    (the prefix is stripped) or plain relative paths like ``foo.py``.

    Raises ValueError if the resolved path escapes the thread directory.
    """
    base = current_thread_dir()

    cleaned = str(rel_path).strip().replace("\\", "/")
    # Strip a leading virtual workspace prefix the agent may include.
    for prefix in ("/workspace/", "workspace/"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    cleaned = cleaned.lstrip("/")

    target = (base / cleaned).resolve()
    if base != target and base not in target.parents:
        raise ValueError(
            f"Path '{rel_path}' resolves outside the thread workspace."
        )
    return target
