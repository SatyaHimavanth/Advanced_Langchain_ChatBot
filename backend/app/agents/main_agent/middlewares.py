"""
middlewares.py
──────────────
Middleware stack for the main (canvas / coding) agent.

This is the heart of the claude.ai-style experience:

  • UserScopedShellMiddleware      — compile / run code in a per-user shell,
                                     scoped to WORKSPACE_ROOT/{tenant}/{user}/.
  • FilesystemMiddleware           — read/write/edit files. /workspace/ maps to
                                     the SAME on-disk directory the shell uses,
                                     so files the agent writes can be compiled.
  • UserScopedFileSearchMiddleware — glob/grep confined to the user's workspace.
  • SkillsMiddleware               — load Agent Skills from the .agents/skills dir.
  • SubAgentMiddleware             — delegate info gathering / exploration to
                                     the websearch, weather and explorer subagents.
  • TodoList / Summarization / ContextEditing / PII / retries — reliability + hygiene.
"""

import re
from pathlib import Path

from langchain.chat_models import BaseChatModel
from langchain.agents.middleware import (
    SummarizationMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    PIIMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
    ModelRetryMiddleware,
    ModelFallbackMiddleware,
    ContextEditingMiddleware,
    ClearToolUsesEdit,
    HostExecutionPolicy,
    HumanInTheLoopMiddleware,
)
from deepagents.backends import CompositeBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware

from app.agents.shared.custom_middlewares.user_scoped_workspace import (
    UserScopedShellMiddleware,
    UserScopedFileSearchMiddleware,
)
from app.agents.shared.custom_middlewares.skills_middleware import SkillsMiddleware
from app.agents.shared.custom_middlewares.user_memory_middleware import UserMemoryMiddleware
from app.agents.shared.custom_middlewares.mcp_prompt_middleware import MCPPromptMiddleware
from app.agents.main_agent.subagents import create_subagents
from app.agents.main_agent.prompts import FILESYSTEM_PROMPT
from app.agents.shared.custom_middlewares.user_scoped_workspace import _runtime_config
from app.settings import settings
from app.logger import get_logger

logger = get_logger(__name__)

WORKSPACE_ROOT = Path(settings.WORKSPACE_ROOT)

# Skills are resolved at invocation time across three priority levels:
#   1. platform  — WORKSPACE_ROOT/.skills                      (shared defaults, lowest priority)
#   2. tenant    — WORKSPACE_ROOT/tenants/{tid}/.skills        (org-level overrides)
#   3. user      — WORKSPACE_ROOT/users/{tid}/{uid}/.skills    (personal, highest priority)
# Missing directories are silently skipped, so new users automatically
# get platform + tenant skills without any provisioning step.
def _skills_dirs(rt) -> list:
    c = _runtime_config(rt).get("configurable", {})
    tenant_id = c.get("tenant_id", "default")
    user_id   = c.get("user_id", "")
    dirs = [
        WORKSPACE_ROOT / "tenants" / tenant_id / ".skills",
    ]
    if user_id:
        dirs.append(WORKSPACE_ROOT / "users" / tenant_id / user_id / ".skills")
    return dirs

_phone_pattern = re.compile(r"\+?[1-9]\d{7,14}")


def _phone_detector(text: str):
    return list(_phone_pattern.finditer(text))


def _try_create_mcp_middleware():
    """Build an MCPPromptMiddleware (+ optional MCP tools) when configured.

    Returns ``(middleware, tools)``. Both are ``None`` / ``[]`` when no MCP
    server is configured or the connection fails.
    """
    server_config = settings.mcp_server_config()
    if not server_config:
        return None, []

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning(
            "MCP_SERVER_URL is set but langchain-mcp-adapters is not installed; "
            "skipping MCPPromptMiddleware."
        )
        return None, []

    try:
        client = MultiServerMCPClient(server_config)
    except Exception:
        logger.exception("Failed to construct MultiServerMCPClient")
        return None, []

    mw = MCPPromptMiddleware(
        client,
        inject_prompt_content=settings.MCP_INJECT_PROMPT_CONTENT,
        include_servers_with_no_prompts=False,
    )
    extra_tools = [mw.prompt_tool]
    logger.info(
        "MCP middleware enabled for servers: %s",
        list(server_config.keys()),
    )
    return mw, extra_tools


def create_middlewares(
    *,
    backend_factory: CompositeBackend,
    llm: BaseChatModel,
    fallback_llm: BaseChatModel | None = None,
    store=None,
):
    logger.info("Creating main-agent middleware stack.")

    mcp_mw, mcp_extra_tools = _try_create_mcp_middleware()

    middlewares = [
        *([ModelFallbackMiddleware(fallback_llm)] if fallback_llm is not None else []),
        # ── Reliability ──────────────────────────────────────────────────
        ModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
        ToolRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
        ModelCallLimitMiddleware(thread_limit=50, run_limit=25),
        ToolCallLimitMiddleware(thread_limit=100, run_limit=50),

        # ── Context hygiene ──────────────────────────────────────────────
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
        # trigger=12k tokens OR 20 messages was too aggressive — the skills
        # catalog alone costs ~2k tokens of system prompt overhead, leaving
        # only ~8k of real conversation before summarization fired.
        # keep must also be LESS than the message trigger, otherwise the
        # middleware preserves more messages than it needs to condense and
        # never actually prunes anything.
        SummarizationMiddleware(
            model=llm,
            trigger=[("tokens", 24_000), ("messages", 40)],
            keep=("messages", 10),
        ),

        # ── Planning ─────────────────────────────────────────────────────
        TodoListMiddleware(),

        # ── Long-term per-user memory (persisted to the Store / Postgres) ─
        # Provides a `remember` tool and auto-injects saved preferences into
        # the system prompt each turn. Only added when a store is available.
        *([UserMemoryMiddleware(store=store)] if store is not None else []),

        # ── PII redaction ────────────────────────────────────────────────
        PIIMiddleware("email", strategy="redact", apply_to_input=True, apply_to_output=True),
        PIIMiddleware("credit_card", strategy="mask", apply_to_input=True, apply_to_output=True),
        PIIMiddleware("phone_number", detector=_phone_detector, strategy="mask"),

        # ── Canvas: shell + filesystem + search (per-user, on disk) ──────
        UserScopedShellMiddleware(
            base_workspace=WORKSPACE_ROOT,
            execution_policy=HostExecutionPolicy(),
        ),
        UserScopedFileSearchMiddleware(
            base_workspace=WORKSPACE_ROOT,
        ),
        FilesystemMiddleware(
            backend=backend_factory,
            system_prompt=FILESYSTEM_PROMPT,
        ),

        # ── Skills ───────────────────────────────────────────────────────
        SkillsMiddleware(skills_dir=_skills_dirs),

        # ── MCP Prompt Catalog ──────────────────────────────────────────
        # Lists prompts from connected MCP servers in the system prompt and
        # exposes a `get_mcp_prompt` tool to render them on demand. Disabled
        # automatically when no MCP server is configured (see settings).
        *([mcp_mw] if mcp_mw is not None else []),

        # ── Human-in-the-loop: gate destructive file deletion ────────────
        # Pauses the run before delete_file executes; the user approves or
        # rejects via the /chat resume flow (interrupt_action). Requires the
        # agent to be built with a checkpointer (it is, in server.py).
        HumanInTheLoopMiddleware(
            interrupt_on={
                "delete_file": {"allowed_decisions": ["approve", "reject"]},
            },
        ),

        # ── Subagents for info gathering / exploration ───────────────────
        SubAgentMiddleware(
            backend=backend_factory,
            subagents=create_subagents(llm=llm, fallback_llm=fallback_llm),
        ),
    ]

    logger.info("Main-agent middleware stack created.")
    return middlewares, mcp_extra_tools