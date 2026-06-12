"""
memory.py
─────────
Agent persistence: LangGraph Store (long-term memory) + Checkpointer
(short-term / thread state).

Two usage modes:

1. ``open_agent_persistence(database_url)`` — an async context manager that
   yields ``(store, checkpointer)``. Use this from the FastAPI lifespan so the
   Postgres connection pools stay open for the app's lifetime. This is the
   preferred path for the running server.

2. ``create_memory_store(database_url)`` — returns just a Store (entering its
   context manager internally for Postgres). Kept for the standalone agent
   factories / ``__main__`` test harnesses where a checkpointer isn't needed.

Table isolation: AsyncPostgresStore/Saver create their own tables
(store, store_migrations, checkpoints, checkpoint_blobs, checkpoint_writes,
checkpoint_migrations). The application DB uses ``app_`` prefixed tables, so
the two never clash even in the same database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlparse

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.logger import get_logger

logger = get_logger(__name__)


def _is_postgres(database_url: str | None) -> bool:
    if not database_url:
        return False
    return urlparse(database_url).scheme.lower() in {"postgres", "postgresql"}


@asynccontextmanager
async def open_agent_persistence(database_url: str | None = None):
    """
    Async context manager yielding ``(store, checkpointer)``.

    For Postgres URLs this opens AsyncPostgresStore + AsyncPostgresSaver and
    runs their one-time ``setup()`` (creates tables / migrations). For anything
    else it falls back to in-memory implementations.
    """
    if not _is_postgres(database_url):
        logger.warning("No Postgres URL configured. Using in-memory store + checkpointer.")
        yield InMemoryStore(), InMemorySaver()
        return

    try:
        from langgraph.store.postgres.aio import AsyncPostgresStore
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError:
        logger.exception(
            "langgraph-checkpoint-postgres is not installed. "
            "Install with: pip install 'langgraph-checkpoint-postgres' 'psycopg[binary,pool]'. "
            "Falling back to in-memory."
        )
        yield InMemoryStore(), InMemorySaver()
        return

    logger.info("Opening AsyncPostgresStore + AsyncPostgresSaver.")
    async with AsyncPostgresStore.from_conn_string(database_url) as store, \
            AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        await store.setup()
        await checkpointer.setup()
        logger.info("Agent persistence ready (Postgres).")
        yield store, checkpointer


async def create_memory_store(database_url: str | None = None) -> BaseStore:
    """
    Return a Store instance. Used by the standalone agent factories.

    NOTE: For Postgres this enters the connection's context manager and leaves
    it open for the process lifetime (suitable for short-lived scripts). The
    running server should use ``open_agent_persistence`` instead.
    """
    if not _is_postgres(database_url):
        logger.warning("No Postgres URL configured. Using InMemoryStore.")
        return InMemoryStore()

    try:
        from langgraph.store.postgres.aio import AsyncPostgresStore

        cm = AsyncPostgresStore.from_conn_string(database_url)
        store = await cm.__aenter__()
        await store.setup()
        logger.info("Initialized AsyncPostgresStore.")
        return store
    except Exception:
        logger.exception("Failed to create Postgres store. Falling back to InMemoryStore.")
        return InMemoryStore()


# Default in-memory checkpointer for standalone use / tests.
checkpointer: BaseCheckpointSaver = InMemorySaver()
