from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import BaseChatModel
from langgraph.store.base import BaseStore

from agents.mail_agent.middlewares import create_middlewares
from agents.mail_agent.prompts import SYSTEM_PROMPT
from agents.mail_agent.tools import tools
from agents.shared.memory import create_memory_store
from logger import get_logger

logger = get_logger(__name__)


async def create_mail_agent(
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

        logger.info("Creating middleware stack.")
        middlewares = create_middlewares(llm=llm)

        logger.info("Creating Mail-Agent...")
        agent = create_agent(
            name="Mail-Agent",
            model=llm,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=middlewares,
            response_format=response_format,
            store=store,
            context_schema=context_schema,
        )
        logger.info("Mail agent created successfully.")
        return agent

    except Exception:
        logger.exception("Error while creating mail agent")
        raise