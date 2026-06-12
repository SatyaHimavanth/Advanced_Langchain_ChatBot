from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from app.agents.main_agent.middlewares import create_middlewares
from app.agents.main_agent.prompts import SYSTEM_PROMPT
from app.agents.main_agent.tools import tools
from app.agents.shared.backend import create_backend
from app.agents.shared.memory import create_memory_store
from app.logger import get_logger

logger = get_logger(__name__)


async def create_main_agent(
    *,
    llm: BaseChatModel,
    context_schema: type | None = None,
    database_url: str | None = None,
    store: BaseStore | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    response_format: Any = None,
):
    """
    Create the main canvas/coding deep agent.

    Args:
        llm: Chat model.
        context_schema: Optional runtime context dataclass (e.g. Context).
        database_url: Used to build a store if one isn't provided.
        store: LangGraph store for long-term memory + filesystem /memory/ route.
        checkpointer: LangGraph checkpointer for thread state (enables resuming
            conversations and HITL interrupts).
        response_format: Optional structured output schema.
    """
    try:
        if store is None:
            logger.info("Initializing store.")
            store = await create_memory_store(database_url)

        logger.info("Creating backend factory.")
        backend_factory = create_backend(store=store)

        logger.info("Creating middleware stack.")
        middlewares, mcp_extra_tools = create_middlewares(
            backend_factory=backend_factory,
            llm=llm,
            store=store,
        )

        agent_tools = list(tools) + list(mcp_extra_tools)

        logger.info("Creating Main-Agent...")
        agent = create_agent(
            name="Main-Agent",
            model=llm,
            tools=agent_tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=middlewares,
            response_format=response_format,
            store=store,
            checkpointer=checkpointer,
            context_schema=context_schema,
        )
        logger.info("Main agent created successfully.")
        return agent

    except Exception:
        logger.exception("Error while creating main agent")
        raise
