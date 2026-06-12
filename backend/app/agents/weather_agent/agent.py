from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import BaseChatModel
from langgraph.store.base import BaseStore

from app.agents.weather_agent.middlewares import create_middlewares
from app.agents.weather_agent.prompts import SYSTEM_PROMPT
from app.agents.weather_agent.tools import tools
from app.agents.shared.memory import create_memory_store
from app.logger import get_logger

logger = get_logger(__name__)


async def create_weather_agent(
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

        logger.info("Creating Weather-Agent...")
        agent = create_agent(
            name="Weather-Agent",
            model=llm,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=middlewares,
            response_format=response_format,
            store=store,
            context_schema=context_schema,
        )
        logger.info("Weather agent created successfully.")
        return agent

    except Exception:
        logger.exception("Error while creating weather agent")
        raise
