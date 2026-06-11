# agents/core/factory.py
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import BaseChatModel
from langgraph.store.base import BaseStore

from agents.core.spec import AgentSpec
from agents.shared.backend import create_backend
from agents.shared.memory import create_memory_store

logger = logging.getLogger(__name__)


MiddlewareFactory = Callable[..., list[Any]]


async def build_agent(
    *,
    spec: AgentSpec,
    llm: BaseChatModel,
    database_url: str | None = None,
    store: BaseStore | None = None,
    middleware_factory: MiddlewareFactory | None = None,
) -> Any:
    try:
        if store is None:
            logger.info("Initializing store.")
            store = await create_memory_store(database_url)

        backend_factory = create_backend(store=store)

        middlewares = ()
        if middleware_factory is None:
            logger.info("Creating middleware stack for %s.", spec.name)
            middlewares = middleware_factory(
                backend_factory=backend_factory,
                llm=llm,
            )

        logger.info("Creating agent: %s", spec.name)
        agent = create_agent(
            name=spec.name,
            model=llm,
            tools=spec.tools,
            system_prompt=spec.system_prompt,
            middleware=middlewares,
            response_format=spec.response_format,
            context_schema=spec.context_schema,
            store=store,
        )

        logger.info("Agent created successfully: %s", spec.name)
        return agent

    except Exception:
        logger.exception("Failed to create agent: %s", spec.name)
        raise