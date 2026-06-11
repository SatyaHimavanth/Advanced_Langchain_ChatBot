from urllib.parse import urlparse

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from logger import get_logger

logger = get_logger(__name__)

checkpointer: BaseCheckpointSaver = InMemorySaver()


async def create_memory_store(database_url: str | None = None) -> BaseStore:
    if not database_url:
        logger.warning("No database URL configured. Using InMemoryStore.")
        return InMemoryStore()

    scheme = urlparse(database_url).scheme.lower()

    try:
        logger.info("Initializing memory store. scheme=%s", scheme)

        if scheme in {"postgres", "postgresql"}:
            from langgraph.store.postgres.aio import AsyncPostgresStore

            store = AsyncPostgresStore.from_conn_string(database_url)
            await store.setup()
            logger.info("Initialized AsyncPostgresStore successfully.")
            return store

        logger.warning("Unsupported database scheme '%s'. Using InMemoryStore.", scheme)
        return InMemoryStore()

    except Exception:
        logger.exception("Error while creating memory store. Falling back to InMemoryStore.")
        return InMemoryStore()
