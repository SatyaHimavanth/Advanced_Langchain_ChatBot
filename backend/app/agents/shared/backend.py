"""
backend.py
──────────
Single entry point for the DeepAgents CompositeBackend used by every agent.

Delegates to the per-user backend factory in
``custom_middlewares.user_scoped_filesystem.create_backend`` which routes:

    /workspace/  → UserScopedFilesystemBackend  (REAL files on disk,
                   scoped to WORKSPACE_ROOT/{tenant}/{user}/)
    /memory/     → StoreBackend                 (persistent per-user memory,
                   backed by AsyncPostgresStore)
    /            → StateBackend                 (ephemeral, in-thread scratch)

This gives the main agent a claude.ai-style canvas: it writes source files to
/workspace/ (on disk) and the shell middleware compiles / runs them from the
same directory.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.store.base import BaseStore
from deepagents.backends import CompositeBackend

from app.agents.shared.custom_middlewares.user_scoped_filesystem import (
    create_backend as _create_user_backend,
)
from app.settings import settings
from app.logger import get_logger

logger = get_logger(__name__)

WORKSPACE_ROOT = Path(settings.WORKSPACE_ROOT)


def create_backend(*, store: BaseStore | None = None) -> CompositeBackend:
    """Create the per-user CompositeBackend for an agent."""
    logger.info("Creating per-user CompositeBackend. workspace_root=%s", WORKSPACE_ROOT)
    return _create_user_backend(
        store=store,
        base_workspace=WORKSPACE_ROOT,
        virtual_mode=True,
    )
