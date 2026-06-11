from pathlib import Path

from langgraph.store.memory import InMemoryStore
from langgraph.store.base import BaseStore
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    StateBackend,
    StoreBackend,
)

from logger import get_logger


logger = get_logger(__name__)


def create_backend(
    *,
    store: BaseStore | None = None,
) -> CompositeBackend:

    logger.info("Creating DeepAgents backend.")

    logger.info("Persistent memory provider: StoreBackend")
    persistent_backend = StoreBackend(
        store=store,
        namespace=lambda rt: (
            "tenant", 
            rt.config.get("configurable", {},).get("tenant_id", "default_tenant",),
            "user", 
            rt.config.get("configurable", {},).get("user_id", "default_user",),
        ),
    )
    
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memory/": persistent_backend,
        },
    )
    logger.info("DeepAgents backend initialized.")

    return backend
