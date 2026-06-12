from langchain.chat_models import BaseChatModel

from langchain.agents.middleware import (
    SummarizationMiddleware,
    ToolRetryMiddleware,
    ModelRetryMiddleware,
)


def create_middlewares(
    *,
    backend_factory=None,
    llm: BaseChatModel,
):
    """Lightweight middleware stack for the basic weather agent."""
    return [
        ModelRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        ToolRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        SummarizationMiddleware(
            model=llm,
            trigger=[
                ("tokens", 12_000),
                ("messages", 20),
            ],
            keep=("messages", 30),
        ),
    ]
