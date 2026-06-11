import re
import logging

from pathlib import Path

from langchain_community.tools import DuckDuckGoSearchRun
from langchain.chat_models import BaseChatModel
from langchain.agents.middleware import (
    SummarizationMiddleware,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ModelFallbackMiddleware,
    PIIMiddleware,
    TodoListMiddleware,
    LLMToolSelectorMiddleware,
    ToolRetryMiddleware,
    ModelRetryMiddleware,
    LLMToolEmulator,
    ContextEditingMiddleware,
    ClearToolUsesEdit,
    ShellToolMiddleware,
    HostExecutionPolicy,
    FilesystemFileSearchMiddleware,
)
from deepagents.backends import CompositeBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware

from agents.coding_agent.tools import web_search
from logger import get_logger


logger = get_logger(__name__)


PROJECT_ROOT = Path.cwd()
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
# WORKSPACE_ROOT.mkdir(exist_ok=True)

phone_pattern = re.compile(
    r"\+?[1-9]\d{7,14}"
)

def phone_detector(text: str):
    return list(phone_pattern.finditer(text))


def create_middlewares(
        *, 
        backend_factory: CompositeBackend, 
        llm: BaseChatModel
    ):
    logging.info("Creating custom middleware for agent...")
    middlewares = [
        # Must keep middlewares
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
            ],
        ),
        SummarizationMiddleware(
            model=llm,
            trigger=[
                    ("tokens", 12_000),
                    ("messages", 20),
                ],
            keep=("messages", 30),
        ),

        # If Agent needs to plan and perform some sequence of steps
        TodoListMiddleware(),

        # If there is content to be hidden
        PIIMiddleware("email", strategy="redact", apply_to_input=True, apply_to_output=True),
        PIIMiddleware("credit_card", strategy="mask", apply_to_input=True, apply_to_output=True),
        PIIMiddleware(
            "phone_number",
            detector=phone_detector,
            strategy="mask",
        ),

        # Tool emulator for expensive or time taking tools
        LLMToolEmulator(tools=["get_weather"]),

        # Coding or system managing agents
        ShellToolMiddleware(
            workspace_root=WORKSPACE_ROOT,
            shell_command=r"C:\Program Files\Git\bin\bash.exe",
            execution_policy=HostExecutionPolicy(),
            # Tell the agent the working directory so it uses absolute paths
            startup_commands=[f"cd '{WORKSPACE_ROOT.as_posix()}'"],
        ),
        FilesystemFileSearchMiddleware(
            root_path=str(WORKSPACE_ROOT),
            use_ripgrep=True,
        ),
        FilesystemMiddleware(
            backend=backend_factory,
            # Restrict FilesystemMiddleware to scratch/memory use only
            system_prompt=(
                "Use ls, read_file, write_file and edit_file ONLY for internal working memory: "
                "storing notes, intermediate results, or plans mid-task. "
                "Do NOT use these tools to produce final output files. "
                "To create a real file on disk that persists after the task, "
                "use the shell tool (e.g. `cat > output.txt << 'EOF'...EOF`)."
            ),
            custom_tool_descriptions={
                "ls": "List your internal scratchpad notes. NOT the real filesystem.",
                "write_file": "Save a working note to your scratchpad. Does NOT create a real file on disk.",
                "read_file": "Read a working note from your scratchpad.",
                "edit_file": "Edit a working note to your scratchpad. Does NOT create a real file on disk.",
            }
        ),

        # use subagents to perform subtasks to assist main agent
        SubAgentMiddleware(
            backend=backend_factory,
            subagents=[
                {
                    "name": "websearch",
                    "description": "This subagent can perform websearch.",
                    "system_prompt": "Use the websearch_tool tool to get the websearch results.",
                    "tools": [web_search],
                    "model": llm,
                    "middleware": [],
                }
            ],
        ),

        # # HITL for sensitive tools
        # HumanInTheLoopMiddleware(
        #     interrupt_on={
        #         "write_file": {
        #             "allowed_decisions": ["approve", "reject"],
        #         },
        #         "edit_file": {
        #             "allowed_decisions": ["approve", "edit", "reject"],
        #         },
        #     }
        # ),
    ]
    logging.info("Created custom middleware for agent.")
    return middlewares
