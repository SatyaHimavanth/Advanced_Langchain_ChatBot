"""
mcp_prompt_middleware.py
─────────────────────────
AgentMiddleware that connects a MultiServerMCPClient to the agent's context,
listing all available MCP prompts from every connected server and:

  1. Injecting a prompt catalog (server name, prompt name, description, all
     parameters with types/required status) into the system prompt via
     `system_prompt_suffix` in `before_model` — so the agent knows what's
     available and how to call each one.

  2. Providing a `get_mcp_prompt` tool the agent can invoke to load a specific
     prompt's rendered content (with argument substitution) at runtime.

  3. Optionally pre-loading argument-free prompts and injecting their rendered
     content into the system prompt directly (like SkillsMiddleware loads skill
     content), so the agent can apply them without calling the tool.

─────────────────────────────────────────────────────────────────────────────
How MCP prompts work (for context)
─────────────────────────────────────────────────────────────────────────────
MCP servers can expose named prompt templates via their prompts capability.
Each prompt has:
  • A name (the function-style identifier used to load it)
  • A description (what it does and when to use it)
  • An arguments list (parameters the caller provides when loading)

Loading a prompt renders the template with the supplied arguments and returns
one or more LangChain messages (SystemMessage, HumanMessage, AIMessage) that
the agent can inject into its context.

─────────────────────────────────────────────────────────────────────────────
Usage
─────────────────────────────────────────────────────────────────────────────

    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain.agents import create_agent
    from mcp_prompt_middleware import MCPPromptMiddleware

    client = MultiServerMCPClient({
        "code_assistant": {
            "transport": "http",
            "url": "http://localhost:8001/mcp",
        },
        "data_tools": {
            "transport": "stdio",
            "command": "python",
            "args": ["/path/to/data_server.py"],
        },
    })

    prompt_mw = MCPPromptMiddleware(client)

    agent = create_agent(
        model="openai:gpt-4.1",
        # Include the tool so the agent can load prompt content at runtime.
        # get_tools() covers MCP tool functions; prompt_mw.prompt_tool adds
        # the catalog-lookup tool on top.
        tools=await client.get_tools() + [prompt_mw.prompt_tool],
        middleware=[
            prompt_mw,
            # ... other middleware (ShellTool, FileSearch, Skills, etc.)
        ],
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Review this Python code: ..."}]}
    )

─────────────────────────────────────────────────────────────────────────────
What the agent sees in its system prompt
─────────────────────────────────────────────────────────────────────────────

    ## MCP Prompt Catalog

    Prompt templates from connected MCP servers. Use the `get_mcp_prompt` tool
    to load any prompt's content with the required arguments.

    ### Server: code_assistant  (2 prompts)

    #### `code_review`
    **Description**: Comprehensive code review with actionable suggestions.
    **Parameters**:
      - `language` *(required)* — Programming language (e.g. "python", "typescript")
      - `code`     *(required)* — Source code to review
      - `focus`    *(optional)* — Focus area: "security" | "performance" | "style"
    **Load**: get_mcp_prompt(server_name="code_assistant",
                              prompt_name="code_review",
                              arguments={"language": "python", "code": "..."})

    #### `summarize`
    **Description**: Produce a concise summary of the supplied text.
    **Parameters**:
      - `content`    *(required)* — Text to summarize
      - `max_length` *(optional)* — Maximum word count
    **Load**: get_mcp_prompt(server_name="code_assistant",
                              prompt_name="summarize",
                              arguments={"content": "..."})

    ### Server: data_tools  (0 prompts)
    No prompts registered on this server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    _HAS_MCP_ADAPTERS = True
except ImportError:
    MultiServerMCPClient = Any  # type: ignore[misc,assignment]
    _HAS_MCP_ADAPTERS = False

try:
    from langchain.tools import StructuredTool
    from pydantic import BaseModel, Field as PField
    _HAS_PYDANTIC = True
except ImportError:
    _HAS_PYDANTIC = False


# ══════════════════════════════════════════════════════════════════════════════
#  PART 1 — Typed representations of MCP prompt metadata
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MCPPromptArgument:
    """A single parameter of an MCP prompt template."""
    name:        str
    description: str   = ""
    required:    bool  = False

    @classmethod
    def from_mcp(cls, arg: Any) -> "MCPPromptArgument":
        """Build from an `mcp.types.PromptArgument` object."""
        return cls(
            name=getattr(arg, "name", str(arg)),
            description=getattr(arg, "description", "") or "",
            required=bool(getattr(arg, "required", False)),
        )


@dataclass
class MCPPromptInfo:
    """
    Metadata about one MCP prompt (name, description, arguments).
    Does NOT contain rendered content — that is fetched on-demand via the tool.
    """
    server_name:  str
    name:         str
    description:  str              = ""
    arguments:    list[MCPPromptArgument] = field(default_factory=list)

    @property
    def has_required_args(self) -> bool:
        return any(a.required for a in self.arguments)

    @property
    def required_args(self) -> list[MCPPromptArgument]:
        return [a for a in self.arguments if a.required]

    @property
    def optional_args(self) -> list[MCPPromptArgument]:
        return [a for a in self.arguments if not a.required]

    def usage_hint(self) -> str:
        """One-line example call for the get_mcp_prompt tool."""
        required_example = {
            a.name: f"<{a.name}>" for a in self.required_args
        }
        hint = (
            f'get_mcp_prompt(server_name="{self.server_name}", '
            f'prompt_name="{self.name}"'
        )
        if required_example:
            hint += f", arguments={required_example}"
        hint += ")"
        return hint

    @classmethod
    def from_mcp(cls, server_name: str, prompt: Any) -> "MCPPromptInfo":
        """Build from an `mcp.types.Prompt` object."""
        args = [
            MCPPromptArgument.from_mcp(a)
            for a in (getattr(prompt, "arguments", None) or [])
        ]
        return cls(
            server_name=server_name,
            name=getattr(prompt, "name", str(prompt)),
            description=getattr(prompt, "description", "") or "",
            arguments=args,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  PART 2 — Prompt catalog cache
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _CatalogEntry:
    prompts:   list[MCPPromptInfo]
    fetched_at: float   # time.monotonic()


class _PromptCatalogCache:
    """
    Thread/asyncio-safe in-process cache of MCP prompt listings.
    Keyed by server_name; invalidated after `ttl_seconds`.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl      = ttl_seconds
        self._entries: dict[str, _CatalogEntry] = {}

    def get(self, server_name: str) -> list[MCPPromptInfo] | None:
        entry = self._entries.get(server_name)
        if entry is None:
            return None
        if self._ttl > 0 and (time.monotonic() - entry.fetched_at) > self._ttl:
            del self._entries[server_name]
            return None
        return entry.prompts

    def put(self, server_name: str, prompts: list[MCPPromptInfo]) -> None:
        self._entries[server_name] = _CatalogEntry(
            prompts=prompts, fetched_at=time.monotonic()
        )

    def invalidate(self, server_name: str | None = None) -> None:
        if server_name is None:
            self._entries.clear()
        else:
            self._entries.pop(server_name, None)


# ══════════════════════════════════════════════════════════════════════════════
#  PART 3 — Server name discovery
# ══════════════════════════════════════════════════════════════════════════════

def _discover_server_names(client: Any) -> list[str]:
    """
    Try to discover server names from the MultiServerMCPClient.
    Attempts several attribute names used by different versions of
    langchain-mcp-adapters; falls back to an empty list.
    """
    for attr in ("connections", "_connections", "servers", "_servers", "server_configs"):
        value = getattr(client, attr, None)
        if isinstance(value, dict):
            names = list(value.keys())
            logger.debug("MCPPromptMiddleware: discovered servers via .%s: %s", attr, names)
            return names
    # Last resort: try iterating the client itself
    try:
        names = list(client)
        if names and all(isinstance(n, str) for n in names):
            return names
    except TypeError:
        pass
    logger.warning(
        "MCPPromptMiddleware: could not auto-discover server names from "
        "MultiServerMCPClient. Pass `server_names` explicitly."
    )
    return []


# ══════════════════════════════════════════════════════════════════════════════
#  PART 4 — Fetch helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _list_prompts_from_server(
    client: Any,
    server_name: str,
) -> list[MCPPromptInfo]:
    """
    Open a session to `server_name` and list its available prompts.
    Returns an empty list (and logs a warning) on any connection error.
    """
    try:
        async with client.session(server_name) as session:
            result = await session.list_prompts()
            prompts = getattr(result, "prompts", None) or []
            return [MCPPromptInfo.from_mcp(server_name, p) for p in prompts]
    except Exception as exc:
        logger.warning(
            "MCPPromptMiddleware: failed to list prompts from server %r: %s",
            server_name, exc,
        )
        return []


async def _render_prompt_content(
    client: Any,
    server_name: str,
    prompt_name: str,
    arguments: dict[str, str] | None,
) -> str:
    """
    Load a prompt from `server_name` with the given arguments and format
    the returned LangChain messages as a readable string block.
    """
    try:
        messages = await client.get_prompt(
            server_name,
            prompt_name,
            arguments or {},
        )
        if not messages:
            return f"[Prompt '{prompt_name}' returned no content.]"

        parts: list[str] = []
        for msg in messages:
            role = getattr(msg, "type", type(msg).__name__).upper()
            if hasattr(msg, "content"):
                content = msg.content
            else:
                content = str(msg)

            if isinstance(content, list):
                # Multi-part content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        text_parts.append(block.get("text", str(block)))
                    else:
                        text_parts.append(str(block))
                content = "\n".join(text_parts)

            parts.append(f"[{role}]\n{content}")

        return "\n\n---\n\n".join(parts)

    except Exception as exc:
        return (
            f"[Error loading prompt '{prompt_name}' from server '{server_name}': {exc}]"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  PART 5 — get_mcp_prompt tool factory
# ══════════════════════════════════════════════════════════════════════════════

def _build_prompt_tool(client: Any, middleware: "MCPPromptMiddleware") -> Any:
    """
    Build a LangChain StructuredTool wrapping the MCP prompt-loading call.
    The tool is added to the agent's tool list so the agent can load prompt
    content on demand with the correct arguments.
    """
    if not _HAS_PYDANTIC:
        raise ImportError(
            "pydantic is required to build the MCP prompt tool. "
            "pip install pydantic"
        )

    class _GetMCPPromptInput(BaseModel):
        server_name: str = PField(
            description=(
                "Name of the MCP server that owns the prompt. "
                "Must match a server registered in the MultiServerMCPClient."
            )
        )
        prompt_name: str = PField(
            description=(
                "Name of the prompt to load, as shown in the MCP Prompt Catalog "
                "section of the system prompt."
            )
        )
        arguments: dict[str, str] | None = PField(
            default=None,
            description=(
                "Key-value arguments to render the prompt template. "
                "Required arguments are listed in the catalog. "
                "Example: {\"language\": \"python\", \"code\": \"def foo(): ...\"}"
            ),
        )

    async def _run(
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ) -> str:
        return await _render_prompt_content(client, server_name, prompt_name, arguments)

    tool_description = (
        "Load the rendered content of an MCP prompt template from a connected server.\n"
        "Use this when the system prompt lists a prompt in the MCP Prompt Catalog "
        "that is relevant to the current task.\n"
        "Returns the prompt's messages formatted as text that you can apply to your "
        "response or use as instructions.\n"
        "Always check the catalog for required vs optional arguments before calling."
    )

    return StructuredTool.from_function(
        coroutine=_run,
        name="get_mcp_prompt",
        description=tool_description,
        args_schema=_GetMCPPromptInput,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PART 6 — System prompt builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_catalog_prompt(
    catalog: dict[str, list[MCPPromptInfo]],
    preloaded_content: dict[str, str],    # "server/prompt_name" → rendered str
    budget_chars: int = 16_384,
) -> str:
    """
    Format the MCP prompt catalog for injection into the system prompt.

    `preloaded_content` holds pre-rendered content for argument-free prompts
    (when `inject_prompt_content=True`).  Key format: "server_name/prompt_name".
    """
    servers = sorted(catalog.keys())
    total_prompts = sum(len(v) for v in catalog.values())

    if total_prompts == 0 and not catalog:
        return ""

    lines: list[str] = [
        "## MCP Prompt Catalog",
        "",
        "Prompt templates from connected MCP servers. "
        "Use the `get_mcp_prompt` tool to load any prompt's content "
        "with the required arguments.",
        "",
    ]

    chars_used = sum(len(ln) + 1 for ln in lines)
    content_budget_left = budget_chars - chars_used

    for server_name in servers:
        prompts = catalog.get(server_name, [])
        count_label = f"{len(prompts)} prompt{'s' if len(prompts) != 1 else ''}"
        server_header = f"### Server: `{server_name}`  ({count_label})"

        if chars_used + len(server_header) > budget_chars:
            lines.append(f"\n*… remaining servers omitted (catalog budget reached)*")
            break

        lines.append(server_header)
        chars_used += len(server_header) + 1

        if not prompts:
            msg = "No prompts registered on this server.\n"
            lines.append(msg)
            chars_used += len(msg)
            continue

        lines.append("")

        for prompt in prompts:
            # ── Prompt heading ────────────────────────────────────────────────
            heading = f"#### `{prompt.name}`"

            # ── Description ───────────────────────────────────────────────────
            desc_line = (
                f"**Description**: {prompt.description}"
                if prompt.description
                else "**Description**: *(not provided)*"
            )

            # ── Arguments ─────────────────────────────────────────────────────
            arg_lines: list[str] = []
            if prompt.arguments:
                arg_lines.append("**Parameters**:")
                for arg in prompt.required_args:
                    arg_desc = f" — {arg.description}" if arg.description else ""
                    arg_lines.append(f"  - `{arg.name}` *(required)*{arg_desc}")
                for arg in prompt.optional_args:
                    arg_desc = f" — {arg.description}" if arg.description else ""
                    arg_lines.append(f"  - `{arg.name}` *(optional)*{arg_desc}")
            else:
                arg_lines.append("**Parameters**: none — call without arguments.")

            # ── Usage hint ────────────────────────────────────────────────────
            usage_line = f"**Load**: `{prompt.usage_hint()}`"

            block_lines = [heading, desc_line] + arg_lines + [usage_line, ""]
            block_text  = "\n".join(block_lines)

            if chars_used + len(block_text) > budget_chars:
                lines.append(
                    f"*… `/{prompt.name}` and subsequent prompts omitted "
                    f"(catalog budget reached)*\n"
                )
                break

            lines.append(block_text)
            chars_used += len(block_text)

            # ── Pre-loaded content (optional) ──────────────────────────────────
            content_key = f"{server_name}/{prompt.name}"
            if content_key in preloaded_content:
                content = preloaded_content[content_key]
                if content_budget_left > 0 and len(content) <= content_budget_left:
                    block = (
                        f"**Pre-loaded content** (no arguments required):\n"
                        f"```\n{content}\n```\n"
                    )
                    lines.append(block)
                    chars_used         += len(block)
                    content_budget_left -= len(content)

    return "\n".join(lines).strip()


def _merge_prompt(parent: dict | None, addition: str) -> dict:
    """Merge a system_prompt_suffix with any existing parent result."""
    if not addition:
        return parent or {}
    if parent is None:
        return {"system_prompt_suffix": addition}
    if isinstance(parent, dict):
        existing = parent.get("system_prompt_suffix", "")
        sep = "\n\n" if existing else ""
        return {**parent, "system_prompt_suffix": existing + sep + addition}
    return {"system_prompt_suffix": addition}


# ══════════════════════════════════════════════════════════════════════════════
#  PART 7 — MCPPromptMiddleware
# ══════════════════════════════════════════════════════════════════════════════

class MCPPromptMiddleware(AgentMiddleware):
    """
    AgentMiddleware that bridges a MultiServerMCPClient's prompt capabilities
    into the agent's context.

    What it does:
      1. At session start (`before_agent`): lists available prompts from every
         connected server and warms the catalog cache.
      2. Before each model call (`before_model`): injects the prompt catalog
         (server names, prompt names, descriptions, parameters) into the system
         prompt so the agent knows what's available.
      3. Provides `prompt_tool` — a `get_mcp_prompt` StructuredTool you add to
         the agent's tools list, which the agent can call to load prompt content
         with argument substitution at runtime.
      4. Optionally pre-loads argument-free prompts and injects their rendered
         content into the system prompt directly.

    Args:
        client
            A `MultiServerMCPClient` instance already configured with server
            connection details.

        server_names
            Explicit list of server names to list prompts from. If None (default),
            auto-discovered from the client's internal connections dict.

        cache_ttl_seconds
            How long to cache prompt listings before re-fetching (default 300 s).
            Set to 0 to disable caching (always re-list on each invocation).

        inject_prompt_content
            If True, pre-render prompts that have zero required arguments and
            inject their content directly into the system prompt (like
            SkillsMiddleware loads skill content). Default False — use the tool.

        catalog_budget_chars
            Maximum characters for the catalog section of the system prompt
            (default 16 384). Entries are truncated if over budget.

        content_budget_chars
            Maximum characters for pre-loaded prompt content blocks when
            `inject_prompt_content=True` (default 8 192).

        include_servers_with_no_prompts
            If True (default), servers with no registered prompts still appear
            in the catalog with a "no prompts" note. Set False to hide them.
    """

    def __init__(
        self,
        client: Any,
        *,
        server_names:                 list[str] | None = None,
        cache_ttl_seconds:            float = 300.0,
        inject_prompt_content:        bool  = False,
        catalog_budget_chars:         int   = 16_384,
        content_budget_chars:         int   = 8_192,
        include_servers_with_no_prompts: bool = True,
    ) -> None:
        if not _HAS_MCP_ADAPTERS:
            raise ImportError(
                "langchain-mcp-adapters is required for MCPPromptMiddleware. "
                "pip install langchain-mcp-adapters"
            )

        self._client                  = client
        self._explicit_servers        = server_names
        self._cache                   = _PromptCatalogCache(ttl_seconds=cache_ttl_seconds)
        self._inject_content          = inject_prompt_content
        self._catalog_budget          = catalog_budget_chars
        self._content_budget          = content_budget_chars
        self._include_empty_servers   = include_servers_with_no_prompts

        # Lazy-built tool — created on first access
        self._prompt_tool_cache: Any | None = None

        logger.info(
            "MCPPromptMiddleware: initialized (servers=%s, inject_content=%s)",
            server_names or "auto-discover",
            inject_prompt_content,
        )

    # ── Server name discovery ─────────────────────────────────────────────────

    def _server_names(self) -> list[str]:
        if self._explicit_servers is not None:
            return list(self._explicit_servers)
        return _discover_server_names(self._client)

    # ── Prompt tool (add to agent tools list) ─────────────────────────────────

    @property
    def prompt_tool(self) -> Any:
        """
        A `get_mcp_prompt` StructuredTool to add to the agent's tools list.

        Usage:
            tools = await client.get_tools() + [prompt_mw.prompt_tool]
            agent = create_agent(model, tools, middleware=[prompt_mw, ...])
        """
        if self._prompt_tool_cache is None:
            self._prompt_tool_cache = _build_prompt_tool(self._client, self)
        return self._prompt_tool_cache

    # ── Catalog fetch ─────────────────────────────────────────────────────────

    async def _warm_catalog(self) -> dict[str, list[MCPPromptInfo]]:
        """
        Fetch prompt listings from all servers (using cache where available).
        Returns a dict: server_name → list[MCPPromptInfo].
        """
        servers  = self._server_names()
        catalog: dict[str, list[MCPPromptInfo]] = {}

        # Fetch missing servers concurrently
        to_fetch = [s for s in servers if self._cache.get(s) is None]

        if to_fetch:
            tasks   = [_list_prompts_from_server(self._client, s) for s in to_fetch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for server_name, result in zip(to_fetch, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "MCPPromptMiddleware: error listing prompts for %r: %s",
                        server_name, result,
                    )
                    prompts: list[MCPPromptInfo] = []
                else:
                    prompts = result  # type: ignore[assignment]

                self._cache.put(server_name, prompts)
                logger.info(
                    "MCPPromptMiddleware: server %r — %d prompt(s) found",
                    server_name, len(prompts),
                )

        for s in servers:
            cached = self._cache.get(s)
            catalog[s] = cached or []

        if not self._include_empty_servers:
            catalog = {k: v for k, v in catalog.items() if v}

        return catalog

    async def _preload_content(
        self, catalog: dict[str, list[MCPPromptInfo]]
    ) -> dict[str, str]:
        """
        For prompts with no required arguments, fetch and cache their content.
        Key format: "server_name/prompt_name".
        """
        preloaded: dict[str, str] = {}
        tasks: list[tuple[str, str, Any]] = []

        for server_name, prompts in catalog.items():
            for prompt in prompts:
                if not prompt.has_required_args:
                    tasks.append((server_name, prompt.name, None))

        if not tasks:
            return preloaded

        results = await asyncio.gather(
            *[
                _render_prompt_content(self._client, s, p, args)
                for s, p, args in tasks
            ],
            return_exceptions=True,
        )

        for (server_name, prompt_name, _), result in zip(tasks, results):
            if not isinstance(result, Exception):
                preloaded[f"{server_name}/{prompt_name}"] = result

        return preloaded

    # ── System prompt builder ─────────────────────────────────────────────────

    async def _make_prompt_suffix(self) -> str:
        catalog = await self._warm_catalog()

        if not any(catalog.values()) and not self._include_empty_servers:
            return ""

        preloaded: dict[str, str] = {}
        if self._inject_content:
            preloaded = await self._preload_content(catalog)

        return _build_catalog_prompt(
            catalog,
            preloaded,
            budget_chars=self._catalog_budget + self._content_budget,
        )

    # ── AgentMiddleware hooks — async (astream / ainvoke) ────────────────────

    async def abefore_agent(self, state: Any, runtime: Any) -> dict | None:
        """Warm the prompt catalog cache at session start."""
        await self._warm_catalog()
        return None

    async def abefore_model(self, state: Any, runtime: Any) -> dict | None:
        """Inject the prompt catalog into the system prompt before each model call."""
        suffix = await self._make_prompt_suffix()
        if not suffix:
            return None
        parent = await super().abefore_model(state, runtime)
        return _merge_prompt(parent, suffix)

    # ── AgentMiddleware hooks — sync (invoke / stream) ───────────────────────

    def before_agent(self, state: Any, runtime: Any) -> dict | None:
        """Sync version: warm cache via asyncio.run if no event loop is running."""
        try:
            loop = asyncio.get_running_loop()
            # We're inside an async context; schedule and yield
            loop.run_until_complete(self._warm_catalog())
        except RuntimeError:
            asyncio.run(self._warm_catalog())
        return None

    def before_model(self, state: Any, runtime: Any) -> dict | None:
        """Sync version: inject catalog using the cached listings."""
        servers  = self._server_names()
        catalog: dict[str, list[MCPPromptInfo]] = {}
        for s in servers:
            cached = self._cache.get(s)
            catalog[s] = cached or []

        if not self._include_empty_servers:
            catalog = {k: v for k, v in catalog.items() if v}

        if not catalog:
            return None

        suffix = _build_catalog_prompt(
            catalog, {}, budget_chars=self._catalog_budget
        )
        if not suffix:
            return None

        parent = super().before_model(state, runtime)
        return _merge_prompt(parent, suffix)

    # ── Convenience ───────────────────────────────────────────────────────────

    def invalidate_cache(self, server_name: str | None = None) -> None:
        """
        Invalidate the catalog cache for one server (or all if server_name is None).
        The next invocation will re-fetch from the MCP server.
        """
        self._cache.invalidate(server_name)
        logger.info(
            "MCPPromptMiddleware: cache invalidated for %s",
            server_name or "all servers",
        )

    async def get_catalog(self) -> dict[str, list[MCPPromptInfo]]:
        """
        Return the current prompt catalog as a dict.
        Useful for debugging or building custom UIs.

            catalog = await prompt_mw.get_catalog()
            for server, prompts in catalog.items():
                for p in prompts:
                    print(f"{server}/{p.name}: {p.description}")
        """
        return await self._warm_catalog()

    async def load_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ) -> str:
        """
        Load a prompt's rendered content directly (without going through the tool).
        Returns a formatted string of the prompt messages.
        """
        return await _render_prompt_content(
            self._client, server_name, prompt_name, arguments
        )


# ══════════════════════════════════════════════════════════════════════════════
#  PART 8 — Diagnostics
# ══════════════════════════════════════════════════════════════════════════════

async def print_mcp_prompt_report(client: Any) -> None:
    """
    Print a human-readable report of all MCP prompts available from a client.
    Useful for debugging your MCP server setup.

        from langchain_mcp_adapters.client import MultiServerMCPClient
        from mcp_prompt_middleware import print_mcp_prompt_report
        import asyncio

        client = MultiServerMCPClient({...})
        asyncio.run(print_mcp_prompt_report(client))
    """
    servers = _discover_server_names(client)
    print("═" * 60)
    print("  MCPPromptMiddleware — prompt report")
    print("═" * 60)

    if not servers:
        print("  No servers discovered. Pass `server_names` explicitly.")
        return

    for server_name in servers:
        prompts = await _list_prompts_from_server(client, server_name)
        print(f"\n  Server: {server_name}  ({len(prompts)} prompt(s))")
        if not prompts:
            print("    No prompts registered.")
        else:
            for p in prompts:
                print(f"\n    {p.name}")
                if p.description:
                    print(f"      {p.description}")
                if p.arguments:
                    for arg in p.required_args:
                        desc = f" — {arg.description}" if arg.description else ""
                        print(f"      * {arg.name} (required){desc}")
                    for arg in p.optional_args:
                        desc = f" — {arg.description}" if arg.description else ""
                        print(f"      * {arg.name} (optional){desc}")

    print("\n" + "═" * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  PART 9 — Full wiring example (commented)
# ══════════════════════════════════════════════════════════════════════════════
#
# from langchain_mcp_adapters.client import MultiServerMCPClient
# from langchain.agents import create_agent
# from langchain.agents.middleware import HostExecutionPolicy
# from pathlib import Path
# from mcp_prompt_middleware import MCPPromptMiddleware
# from skills_middleware import SkillsMiddleware
# from user_scoped_workspace import UserScopedShellMiddleware, UserScopedFileSearchMiddleware
#
# WORKSPACE = Path("workspace")
#
# # 1. Set up MCP client with all your servers
# client = MultiServerMCPClient({
#     "code_assistant": {
#         "transport": "http",
#         "url": "http://localhost:8001/mcp",
#     },
#     "data_tools": {
#         "transport": "stdio",
#         "command": "python",
#         "args": ["./servers/data_tools.py"],
#     },
# })
#
# # 2. Create the prompt middleware (before create_agent so we can add its tool)
# prompt_mw = MCPPromptMiddleware(
#     client,
#     cache_ttl_seconds=300,
#     inject_prompt_content=False,  # True = pre-load arg-free prompts
#     include_servers_with_no_prompts=False,
# )
#
# # 3. Wire everything together
# mcp_tools  = await client.get_tools()             # MCP server tools
# prompt_tool = prompt_mw.prompt_tool               # get_mcp_prompt tool
#
# coding_agent = create_agent(
#     model=model,
#     tools=[*mcp_tools, prompt_tool],              # include prompt_tool here
#     middleware=[
#         prompt_mw,                                # catalog in system prompt
#         SkillsMiddleware(".agents/skills"),        # skills from .agents/skills/
#         UserScopedShellMiddleware(WORKSPACE),      # per-user shell
#         UserScopedFileSearchMiddleware(WORKSPACE), # per-user file search
#         FilesystemMiddleware(backend=backend),     # virtual FS with StoreBackend
#     ],
#     checkpointer=InMemorySaver(),
# )
#
# # 4. Invoke with per-user config (same keys used by all middlewares)
# config = {
#     "configurable": {
#         "thread_id": "conv-abc",
#         "tenant_id": "acme",
#         "user_id":   "alice",
#     }
# }
# result = await coding_agent.ainvoke(
#     {"messages": [{"role": "user", "content": "Review my Python auth code."}]},
#     config=config,
# )
#
# # ── Debugging ──────────────────────────────────────────────────────────────
#
# # See what prompts are available
# await print_mcp_prompt_report(client)
#
# # Inspect the catalog at runtime
# catalog = await prompt_mw.get_catalog()
# for server, prompts in catalog.items():
#     print(f"{server}: {[p.name for p in prompts]}")
#
# # Load a prompt directly (outside the agent)
# content = await prompt_mw.load_prompt(
#     "code_assistant",
#     "code_review",
#     {"language": "python", "code": "def foo(): pass"},
# )
# print(content)
