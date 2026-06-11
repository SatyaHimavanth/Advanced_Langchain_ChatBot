from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import BaseChatModel
from langgraph.store.base import BaseStore

from agents.coding_agent.middlewares import create_middlewares
from agents.coding_agent.prompts import SYSTEM_PROMPT
from agents.coding_agent.tools import tools
from agents.shared.backend import create_backend
from agents.shared.memory import create_memory_store
from logger import get_logger

logger = get_logger(__name__)


async def create_coding_agent(
    *,
    llm: BaseChatModel,
    context_schema: type | None = None,
    database_url: str | None = None,
    store: BaseStore | None = None,
    response_format: Any = None,
):
    try:
        if store is None:
            logger.info("Initializing store.")
            store = await create_memory_store(database_url)

        logger.info("Creating backend factory.")
        backend_factory = create_backend(store=store)

        logger.info("Creating middleware stack.")
        middlewares = create_middlewares(
            backend_factory=backend_factory,
            llm=llm,
        )

        logger.info("Creating Coding-Agent...")
        agent = create_agent(
            name="Coding-Agent",
            model=llm,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=middlewares,
            response_format=response_format,
            store=store,
            context_schema=context_schema,
        )
        logger.info("Coding agent created successfully.")
        return agent

    except Exception:
        logger.exception("Error while creating coding agent")
        raise