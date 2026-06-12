from langchain.chat_models import BaseChatModel

from langchain.agents.middleware import (
    SummarizationMiddleware,
    ToolRetryMiddleware,
    ModelRetryMiddleware,
    LLMToolEmulator,
    ContextEditingMiddleware,
    ClearToolUsesEdit,
)


def create_middlewares(
    *,
    backend_factory=None,
    llm: BaseChatModel,
):
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
        ContextEditingMiddleware(
            edits=[
                ClearToolUsesEdit(
                    trigger=2000,
                    keep=3,
                    clear_tool_inputs=False,
                    exclude_tools=[],
                    placeholder="[cleared]",
                ),
            ]
        ),
        SummarizationMiddleware(
            model=llm,
            trigger=[
                ("tokens", 12_000),
                ("messages", 20),
            ],
            keep=("messages", 30),
        ),
        LLMToolEmulator(
            tools=["send_mail"]
        ),
    ]