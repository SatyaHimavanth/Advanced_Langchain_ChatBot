"""
Demonstrates every major FastMCP feature in one cohesive codebase:

  Section 1  Custom HTTP routes       (health / readiness probes)
  Section 2  Public tools             (echo, calculate, server_info)
  Section 3  Bearer-token tools       (list/get/create/update/delete tasks)
  Section 4  Bearer-token user tools  (get_user_profile)
  Section 5  Resources                (static + resource template)
  Section 6  Prompts                  (multi-message, with typed arguments)

Cross-cutting concerns (auth, logging, rate-limiting, scope enforcement,
response capping) live in the middleware stack assembled below.

Run:
  python server.py                    # HTTP on port 8000
  fastmcp dev server.py              # Inspector UI
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from fastmcp import Context, FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.rate_limiting import SlidingWindowRateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from auth import DEV_TOKENS, build_auth_provider
from config import settings
from middleware import AuditLogMiddleware, AuthContextMiddleware, ScopeEnforcementMiddleware

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.is_development else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("taskhub")

# ── In-memory store ───────────────────────────────────────────────────────────
# Swap for a real DB (SQLAlchemy, motor, etc.) via the lifespan below.
_tasks: dict[str, dict[str, Any]] = {}
_users: dict[str, dict[str, Any]] = {}


# ═════════════════════════════════════════════════════════════════════════════
# LIFESPAN  —  startup / shutdown hooks
# ═════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastMCP):
    """
    Seed demo data on startup; release resources on shutdown.
    In production: open DB connection pool here, close it in the finally block.
    """
    logger.info(
        "🚀  Starting %s v%s [%s]", settings.name, settings.version, settings.environment
    )
    auth_mode = "StaticToken [dev]" if settings.is_development else "JWT [prod]"
    logger.info("    Auth mode  : %s", auth_mode)
    logger.info("    Transport  : HTTP %s:%s", settings.host, settings.port)

    # Seed users
    _users.update({
        "user-001": {
            "id": "user-001", "name": "Alice",
            "email": "alice@company.com", "role": "admin",
        },
        "user-002": {
            "id": "user-002", "name": "Bob",
            "email": "bob@company.com",   "role": "user",
        },
        "user-003": {
            "id": "user-003", "name": "Charlie",
            "email": "charlie@company.com", "role": "viewer",
        },
    })

    # Seed tasks
    _tasks.update({
        "task-001": {
            "id": "task-001", "title": "Set up CI/CD pipeline",
            "status": "in_progress", "priority": "high",
            "assignee": "user-001", "tags": ["devops", "infrastructure"],
            "created_by": "system", "created_at": "2024-01-15T09:00:00Z",
        },
        "task-002": {
            "id": "task-002", "title": "Write unit tests for auth module",
            "status": "todo", "priority": "medium",
            "assignee": "user-002", "tags": ["testing", "quality"],
            "created_by": "user-001", "created_at": "2024-01-16T10:30:00Z",
        },
        "task-003": {
            "id": "task-003", "title": "Update API documentation",
            "status": "done", "priority": "low",
            "assignee": "user-001", "tags": ["docs"],
            "created_by": "user-001", "created_at": "2024-01-10T08:00:00Z",
        },
    })

    logger.info("✅  Seeded %d tasks, %d users", len(_tasks), len(_users))

    yield  # ← server is live between yield and the finally block

    logger.info("🛑  Shutting down — releasing resources")
    _tasks.clear()
    _users.clear()


# ═════════════════════════════════════════════════════════════════════════════
# SERVER ASSEMBLY
# ═════════════════════════════════════════════════════════════════════════════

mcp = FastMCP(
    # ── Identity ──────────────────────────────────────────────────────────────
    name=settings.name,
    version=settings.version,
    instructions="""
TaskHub MCP — Team task management server.

AUTHENTICATION
  Every connection requires an Authorization: Bearer <token> header.

  Development tokens (ENVIRONMENT=development):
    dev-admin-2024    → all scopes  (admin role)
    dev-user-2024     → tasks:read/write, users:read  (user role)
    dev-readonly-2024 → tasks:read only  (viewer role)

  Production: provide a valid HS256-signed JWT with the correct iss/aud.
  Run `python generate_token.py --help` to mint test tokens.

SCOPE REFERENCE
  tasks:read   list_tasks, get_task
  tasks:write  create_task, update_task
  tasks:admin  delete_task
  users:read   get_user_profile

PUBLIC TOOLS (token required to connect, no extra scope needed)
  echo · calculate · server_info
""",

    # ── Auth ──────────────────────────────────────────────────────────────────
    auth=build_auth_provider(),

    # ── Middleware pipeline ────────────────────────────────────────────────────
    # Order is outermost-first: the first entry wraps all subsequent ones.
    # Request flows  →  A → B → C → handler
    # Response flows →  handler → C → B → A
    middleware=[
        # 1. Catch & log all exceptions — must be outermost to wrap everything
        ErrorHandlingMiddleware(
            include_traceback=settings.is_development,
            transform_errors=True,
        ),
        # 2. Structured JSON logs → Datadog / Splunk / CloudWatch
        StructuredLoggingMiddleware(),
        # 3. Per-operation timing metrics
        TimingMiddleware(),
        # 4. Throttle per client before any heavy work
        SlidingWindowRateLimitingMiddleware(
            max_requests=settings.rate_limit_max_requests,
            window_minutes=settings.rate_limit_window_minutes,
        ),
        # 5. Decode JWT / static token → write user_id, user_scopes to session state
        AuthContextMiddleware(
            secret=settings.jwt_secret,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithm=settings.jwt_algorithm,
            static_tokens=DEV_TOKENS,   # dev opaque-token passthrough
        ),
        # 6. Enforce scope:* tags declared on individual tools
        ScopeEnforcementMiddleware(),
        # 7. Structured per-tool audit log (fires after scopes are resolved)
        AuditLogMiddleware(),
        # 8. Hard-cap response size to protect LLM context windows
        ResponseLimitingMiddleware(max_size=settings.max_response_bytes),
    ],

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    lifespan=lifespan,

    # ── Behaviour ─────────────────────────────────────────────────────────────
    # Hide internal stack traces from LLM clients in production.
    mask_error_details=settings.mask_error_details,
    # Server-driven pagination for list operations.
    list_page_size=settings.list_page_size,
    # Crash on accidental duplicate tool registration — catches copy-paste bugs.
    on_duplicate="error",
    # Allow LLM type coercion ("3" → 3 for int params) — most clients need this.
    strict_input_validation=False,
)

# ── Tag-based visibility ──────────────────────────────────────────────────────
# Components tagged "experimental" or "deprecated" are hidden from all clients
# by default. Re-enable per-session in a tool, or per-environment here.
mcp.disable(tags={"experimental", "deprecated"})


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1  —  CUSTOM HTTP ROUTES
# These are plain Starlette routes served alongside the MCP endpoint.
# Use for liveness / readiness probes and simple webhooks.
# ═════════════════════════════════════════════════════════════════════════════

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Kubernetes / Docker liveness probe. Returns 200 if the process is up."""
    return JSONResponse({
        "status": "healthy",
        "server":      settings.name,
        "version":     settings.version,
        "environment": settings.environment,
    })


@mcp.custom_route("/ready", methods=["GET"])
async def readiness_check(request: Request) -> JSONResponse:
    """
    Readiness probe.
    Returns 503 until the lifespan has seeded data (signals: not yet ready
    to receive traffic after a cold start or a rolling deployment).
    """
    if not _tasks:
        return JSONResponse(
            {"status": "not_ready", "reason": "data not loaded"},
            status_code=503,
        )
    return JSONResponse({
        "status":       "ready",
        "tasks_loaded": len(_tasks),
        "users_loaded": len(_users),
    })


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2  —  PUBLIC TOOLS
# A valid bearer token is required to connect; no extra scope is enforced.
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    tags={"public", "utility"},
    annotations=ToolAnnotations(
        title="Echo Message",
        readOnlyHint=True,
        openWorldHint=False,
    ),
)
def echo(
    message: Annotated[str, "Text to echo back"],
    repeat:  Annotated[int, "Number of times to repeat (1–5, default 1)"] = 1,
) -> str:
    """
    Echo a message back, optionally repeated.

    Args:
        message: Text to return.
        repeat: Repetition count — clamped to [1, 5].
    """
    return " ".join([message] * max(1, min(5, repeat)))


@mcp.tool(
    tags={"public", "utility"},
    annotations=ToolAnnotations(
        title="Safe Calculator",
        readOnlyHint=True,
        openWorldHint=False,
        idempotentHint=True,
    ),
    timeout=5.0,    # Never block the server on a user-supplied expression
)
def calculate(
    expression: Annotated[
        str,
        "Math expression to evaluate. Supports: + - * / ** and parentheses. "
        "Example: '(12 + 8) ** 2 / 4'",
    ],
) -> dict[str, Any]:
    """
    Safely evaluate a simple mathematical expression.

    Only digits, the four arithmetic operators, ** (power), and parentheses
    are allowed — everything else raises ValueError.

    Args:
        expression: The expression string to evaluate.

    Returns:
        dict with 'expression', 'result', and 'type'.
    """
    allowed = set("0123456789+-**/()., \te")
    if not set(expression).issubset(allowed):
        raise ValueError(
            "Unsafe expression. Only digits, +−*/**, and parentheses are allowed."
        )
    try:
        result = eval(expression, {"__builtins__": {}}, {})  # noqa: PGH001 S307
    except ZeroDivisionError:
        raise ValueError("Division by zero.")
    except Exception as exc:
        raise ValueError(f"Invalid expression: {exc}") from exc

    return {
        "expression": expression,
        "result":     result,
        "type":       type(result).__name__,
    }


@mcp.tool(
    tags={"public", "meta"},
    annotations=ToolAnnotations(
        title="Server Info",
        readOnlyHint=True,
        openWorldHint=False,
    ),
)
async def server_info(ctx: Context = CurrentContext()) -> dict[str, Any]:
    """
    Return server metadata and the current caller's session details.

    Demonstrates Context:
      - Logging         (ctx.debug / ctx.info)
      - Session state   (ctx.get_state — populated by AuthContextMiddleware)
      - Transport info  (ctx.transport)
      - Request metadata (ctx.request_id, ctx.client_id)
    """
    # ── Context: structured logging ───────────────────────────────────────────
    await ctx.debug("server_info invoked")

    # ── Context: read session state set by AuthContextMiddleware ──────────────
    user_id     = await ctx.get_state("user_id")     or "unauthenticated"
    user_role   = await ctx.get_state("user_role")   or "none"
    user_scopes = sorted(await ctx.get_state("user_scopes") or [])

    # ── Context: request metadata ─────────────────────────────────────────────
    request_id = ctx.request_id if ctx.request_context else "n/a"
    client_id  = ctx.client_id  or "unknown"

    await ctx.info("server_info → user=%s transport=%s", user_id, ctx.transport)

    return {
        "server": {
            "name":        settings.name,
            "version":     settings.version,
            "environment": settings.environment,
        },
        "transport": ctx.transport,
        "session": {
            "request_id": request_id,
            "client_id":  client_id,
        },
        "caller": {
            "user_id":     user_id,
            "role":        user_role,
            "scopes":      user_scopes,
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3  —  TASK TOOLS  (require bearer token with specific scopes)
#
# Scope enforcement is declarative: add "scope:<name>" to a tool's tags and
# ScopeEnforcementMiddleware rejects callers who lack that scope automatically.
# No guard code inside the tool body is needed.
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    tags={"tasks", "scope:tasks:read"},
    annotations=ToolAnnotations(
        title="List Tasks",
        readOnlyHint=True,
        idempotentHint=True,
    ),
)
async def list_tasks(
    status:   Annotated[str | None, "Filter by status: todo | in_progress | done"] = None,
    priority: Annotated[str | None, "Filter by priority: low | medium | high"]     = None,
    ctx: Context = CurrentContext(),
) -> list[dict[str, Any]]:
    """
    List all tasks, optionally filtered by status and/or priority.
    Requires scope: tasks:read.

    Demonstrates Context:
      - ctx.info / ctx.report_progress for long-running awareness
    """
    await ctx.info("list_tasks(status=%r, priority=%r)", status, priority)

    _STATUSES   = {"todo", "in_progress", "done"}
    _PRIORITIES = {"low", "medium", "high"}

    if status   and status   not in _STATUSES:
        raise ValueError(f"Invalid status '{status}'. Choose from: {', '.join(_STATUSES)}")
    if priority and priority not in _PRIORITIES:
        raise ValueError(f"Invalid priority '{priority}'. Choose from: {', '.join(_PRIORITIES)}")

    await ctx.report_progress(progress=0, total=100)

    tasks = list(_tasks.values())
    if status:
        tasks = [t for t in tasks if t["status"]   == status]
    if priority:
        tasks = [t for t in tasks if t["priority"] == priority]

    await ctx.report_progress(progress=100, total=100)
    await ctx.info("Returning %d task(s)", len(tasks))
    return tasks


@mcp.tool(
    tags={"tasks", "scope:tasks:read"},
    annotations=ToolAnnotations(
        title="Get Task",
        readOnlyHint=True,
        idempotentHint=True,
    ),
)
async def get_task(
    task_id: Annotated[str, "Task ID to retrieve (e.g. task-001)"],
    ctx: Context = CurrentContext(),
) -> dict[str, Any]:
    """
    Retrieve a single task by its ID.
    Requires scope: tasks:read.
    """
    await ctx.debug("get_task(task_id=%r)", task_id)
    task = _tasks.get(task_id)
    if not task:
        # ToolError message is forwarded verbatim to the LLM.
        raise ToolError(f"Task '{task_id}' not found.")
    return task


@mcp.tool(
    tags={"tasks", "scope:tasks:write"},
    annotations=ToolAnnotations(
        title="Create Task",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
async def create_task(
    title:    Annotated[str,       "Task title (3–200 characters)"],
    priority: Annotated[str,       "Priority: low | medium | high"] = "medium",
    assignee: Annotated[str | None,"User ID to assign, or omit for unassigned"] = None,
    tags:     Annotated[list[str], "Classification tags"]           = [],  # noqa: B006
    ctx: Context = CurrentContext(),
) -> dict[str, Any]:
    """
    Create a new task.
    Requires scope: tasks:write.

    Demonstrates Context:
      - ctx.get_state("user_id") to stamp the creator field
      - ctx.info for operation logging
    """
    # Input validation — raise ValueError so mask_error_details can handle it.
    if len(title) < 3:
        raise ValueError("Title must be at least 3 characters.")
    if len(title) > 200:
        raise ValueError("Title cannot exceed 200 characters.")
    if priority not in {"low", "medium", "high"}:
        raise ValueError("Priority must be: low, medium, or high.")
    if assignee and assignee not in _users:
        raise ValueError(f"User '{assignee}' does not exist.")

    # ── Context: read caller identity from session state ──────────────────────
    creator = await ctx.get_state("user_id") or "unknown"

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    now     = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    task: dict[str, Any] = {
        "id":         task_id,
        "title":      title,
        "status":     "todo",
        "priority":   priority,
        "assignee":   assignee,
        "tags":       list(tags),
        "created_by": creator,
        "created_at": now,
    }
    _tasks[task_id] = task

    await ctx.info("Task created: %r by user=%r", task_id, creator)
    return task


@mcp.tool(
    tags={"tasks", "scope:tasks:write"},
    annotations=ToolAnnotations(
        title="Update Task",
        readOnlyHint=False,
        destructiveHint=False,
    ),
)
async def update_task(
    task_id:  Annotated[str,       "ID of the task to update"],
    title:    Annotated[str | None,"New title (optional)"]                        = None,
    status:   Annotated[str | None,"New status: todo | in_progress | done"]       = None,
    priority: Annotated[str | None,"New priority: low | medium | high (optional)"]= None,
    ctx: Context = CurrentContext(),
) -> dict[str, Any]:
    """
    Update one or more fields of an existing task.
    Only provided (non-None) fields are modified — partial updates are safe.
    Requires scope: tasks:write.
    """
    task = _tasks.get(task_id)
    if not task:
        raise ToolError(f"Task '{task_id}' not found.")

    patch: dict[str, Any] = {}
    if title    is not None:
        if len(title) < 3:
            raise ValueError("Title must be at least 3 characters.")
        patch["title"]    = title
    if status   is not None:
        if status not in {"todo", "in_progress", "done"}:
            raise ValueError("Invalid status.")
        patch["status"]   = status
    if priority is not None:
        if priority not in {"low", "medium", "high"}:
            raise ValueError("Invalid priority.")
        patch["priority"] = priority

    if not patch:
        raise ValueError("No valid fields provided. Supply at least one of: title, status, priority.")

    editor = await ctx.get_state("user_id") or "unknown"
    task.update({
        **patch,
        "updated_by": editor,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    await ctx.info("Task %r updated by user=%r — fields: %s", task_id, editor, list(patch))
    return task


@mcp.tool(
    tags={"tasks", "scope:tasks:admin"},
    annotations=ToolAnnotations(
        title="Delete Task",
        readOnlyHint=False,
        destructiveHint=True,   # Irreversible — clients should warn users
    ),
)
async def delete_task(
    task_id: Annotated[str, "ID of the task to permanently delete"],
    ctx: Context = CurrentContext(),
) -> dict[str, str]:
    """
    Permanently delete a task. This action cannot be undone.
    Requires scope: tasks:admin.

    Demonstrates Context:
      - ctx.warning for high-severity audit events
    """
    task = _tasks.pop(task_id, None)
    if not task:
        raise ToolError(f"Task '{task_id}' not found or already deleted.")

    deleter = await ctx.get_state("user_id") or "unknown"

    # ── Context: elevated log level for destructive actions ───────────────────
    await ctx.warning("Task %r permanently deleted by user=%r", task_id, deleter)

    return {
        "deleted":    task_id,
        "title":      task.get("title", ""),
        "deleted_by": deleter,
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4  —  USER TOOLS  (require users:read scope)
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    tags={"users", "scope:users:read"},
    annotations=ToolAnnotations(
        title="Get User Profile",
        readOnlyHint=True,
    ),
)
async def get_user_profile(
    user_id: Annotated[str, "User ID to look up. Pass 'me' to retrieve your own profile."],
    ctx: Context = CurrentContext(),
) -> dict[str, Any]:
    """
    Retrieve a user profile.
    Pass 'me' to resolve the currently-authenticated caller automatically.
    Requires scope: users:read.

    Demonstrates Context:
      - 'me' shorthand that resolves from session state
    """
    # ── Context: resolve 'me' alias ───────────────────────────────────────────
    if user_id == "me":
        user_id = await ctx.get_state("user_id") or "unknown"

    user = _users.get(user_id)
    if not user:
        raise ToolError(f"User '{user_id}' not found.")

    await ctx.debug("get_user_profile → user=%r", user_id)
    return user


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5  —  RESOURCES
# Resources expose data passively (read-only pull).
# Tools perform actions (active invocation).
# ═════════════════════════════════════════════════════════════════════════════

@mcp.resource(
    "tasks://all",
    name="All Tasks",
    description="Full task list. Every read returns the live store — no caching.",
    annotations={"readOnlyHint": True},
    tags={"tasks", "scope:tasks:read"},
)
async def resource_all_tasks() -> list[dict[str, Any]]:
    """Returns the complete task list as JSON."""
    return list(_tasks.values())


# Resource Template — URI contains a parameter extracted by FastMCP automatically.
@mcp.resource(
    "tasks://{task_id}",
    name="Task by ID",
    description="Fetch a single task. URI format: tasks://<task_id>  e.g. tasks://task-001",
    annotations={"readOnlyHint": True},
    tags={"tasks", "scope:tasks:read"},
)
async def resource_task_by_id(task_id: str) -> dict[str, Any]:
    """Returns a single task as JSON. Raises ResourceError if not found."""
    task = _tasks.get(task_id)
    if not task:
        raise ResourceError(f"Task '{task_id}' not found.")
    return task


@mcp.resource(
    "config://server",
    name="Server Configuration",
    description="Non-sensitive server config and feature flags. Stable across requests.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
    tags={"meta", "public"},
)
def resource_server_config() -> dict[str, Any]:
    """Returns a snapshot of public server configuration."""
    return {
        "server_name": settings.name,
        "version":     settings.version,
        "environment": settings.environment,
        "features": {
            "task_management": True,
            "user_profiles":   True,
        },
        "limits": {
            "max_response_bytes": settings.max_response_bytes,
            "list_page_size":     settings.list_page_size,
        },
    }


@mcp.resource(
    "docs://api",
    name="API Reference",
    description="Markdown reference for all TaskHub MCP tools, scopes, and resources.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
    tags={"docs", "public"},
)
def resource_api_docs() -> str:
    """Full API reference as Markdown."""
    return """
# TaskHub MCP — API Reference

## Authentication
Every request requires: `Authorization: Bearer <token>`

## Scope Reference
| Scope        | Tools                              |
|--------------|------------------------------------|
| tasks:read   | list_tasks, get_task               |
| tasks:write  | create_task, update_task           |
| tasks:admin  | delete_task                        |
| users:read   | get_user_profile                   |

## Public Tools (no extra scope; token still required to connect)
| Tool          | Description                               |
|---------------|-------------------------------------------|
| echo          | Echo text back, optionally repeated       |
| calculate     | Safe math expression evaluator            |
| server_info   | Server metadata + caller identity         |

## Task Tools
| Tool          | Scope          | Description                  |
|---------------|----------------|------------------------------|
| list_tasks    | tasks:read     | List/filter tasks            |
| get_task      | tasks:read     | Get single task by ID        |
| create_task   | tasks:write    | Create a new task            |
| update_task   | tasks:write    | Partial-update a task        |
| delete_task   | tasks:admin    | Permanently delete a task    |

## User Tools
| Tool              | Scope        | Description             |
|-------------------|--------------|-------------------------|
| get_user_profile  | users:read   | Get profile (use 'me')  |

## Resources
| URI                 | Description                              |
|---------------------|------------------------------------------|
| tasks://all         | All tasks (live)                         |
| tasks://{task_id}   | Single task by ID                        |
| config://server     | Server config & feature flags            |
| docs://api          | This document                            |

## Prompts
| Name            | Description                              |
|-----------------|------------------------------------------|
| summarize_tasks | Prioritised task summary report          |
| code_review     | Focused code review (security/perf/etc.) |
| debug_request   | Root-cause analysis for errors           |
""".strip()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6  —  PROMPTS
# Reusable message templates that guide LLM responses.
# Unlike tools, prompts are rendered server-side and returned as messages.
# ═════════════════════════════════════════════════════════════════════════════

@mcp.prompt(
    name="summarize_tasks",
    description="Generate a prioritised summary and action plan for current tasks.",
    tags={"tasks", "reporting"},
)
async def prompt_summarize_tasks(
    statuses: Annotated[
        list[str],
        "Status filters to include. Example: ['todo', 'in_progress']",
    ] = ["todo", "in_progress"],  # noqa: B006
) -> list[dict[str, str]]:
    """
    Builds a multi-turn prompt asking the LLM to summarise and prioritise
    tasks that match the requested statuses.

    Accesses live _tasks store at render time so the LLM always sees
    up-to-date content.
    """
    filtered = [t for t in _tasks.values() if t.get("status") in statuses]

    if not filtered:
        task_block = "No tasks found for the given status filters."
    else:
        task_block = "\n".join(
            f"- [{t['priority'].upper()}] {t['title']}"
            f"  (status: {t['status']}, id: {t['id']})"
            for t in filtered
        )

    return [
        {
            "role": "user",
            "content": (
                "Please summarise and prioritise the following tasks:\n\n"
                f"{task_block}\n\n"
                "Structure your response as:\n"
                "1. **Executive Summary** (2–3 sentences)\n"
                "2. **Top 3 Priorities** (with rationale)\n"
                "3. **Blocked or At-Risk Items** (if any)\n"
                "4. **Recommended Next Actions**"
            ),
        }
    ]


@mcp.prompt(
    name="code_review",
    description="Structured code review focused on a specific quality dimension.",
    tags={"engineering"},
)
def prompt_code_review(
    code: Annotated[str, "The code snippet to review"],
    language: Annotated[str, "Programming language, e.g. Python, TypeScript"] = "Python",
    focus: Annotated[
        str,
        "Review focus: security | performance | readability | all",
    ] = "all",
) -> list[dict[str, str]]:
    """
    Returns a two-message prompt (system + user) for a focused code review.

    The system message sets the reviewer persona and focus; the user message
    presents the code and requests structured output.
    """
    instructions = {
        "security":    "Focus exclusively on security vulnerabilities, injection risks, and data exposure.",
        "performance": "Focus exclusively on algorithmic complexity, memory usage, and I/O bottlenecks.",
        "readability": "Focus exclusively on clarity, naming, documentation, and long-term maintainability.",
        "all":         "Cover security, performance, readability, correctness, and best practices comprehensively.",
    }.get(focus, "Cover all aspects of code quality.")

    return [
        {
            "role": "system",
            "content": (
                f"You are an expert {language} code reviewer. {instructions} "
                "Be specific, actionable, and cite line-level examples where possible."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Review the following {language} code:\n\n"
                f"```{language.lower()}\n{code}\n```\n\n"
                "Provide:\n"
                "1. **Overall Assessment** (1–2 sentences)\n"
                "2. **Issues Found** (severity: critical | major | minor)\n"
                "3. **Specific Improvements** (with corrected code where useful)\n"
                "4. **Verdict**: Approve / Request Changes / Block"
            ),
        },
    ]


@mcp.prompt(
    name="debug_request",
    description="Root-cause analysis prompt for investigating errors and exceptions.",
    tags={"engineering", "debugging"},
)
def prompt_debug_request(
    error_message: Annotated[str,       "The error message or exception text"],
    context:       Annotated[str,       "What you were doing when the error occurred"],
    code_snippet:  Annotated[str | None,"Relevant code snippet (optional)"] = None,
) -> list[dict[str, str]]:
    """
    Returns a single-user-message prompt that guides the LLM through
    structured root-cause analysis and a step-by-step fix.
    """
    code_section = (
        f"\n\nRelevant code:\n```\n{code_snippet}\n```" if code_snippet else ""
    )
    return [
        {
            "role": "user",
            "content": (
                "I encountered an error and need debugging help.\n\n"
                f"**Error:**\n{error_message}\n\n"
                f"**What I was doing:**\n{context}"
                f"{code_section}\n\n"
                "Please:\n"
                "1. **Explain** the likely cause of this error\n"
                "2. **Identify** the root cause (not just the symptom)\n"
                "3. **Provide** a step-by-step fix with code\n"
                "4. **Suggest** how to prevent this class of error in future"
            ),
        }
    ]


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
    )
