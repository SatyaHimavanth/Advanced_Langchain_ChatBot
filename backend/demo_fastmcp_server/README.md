# TaskHub MCP

A production-ready FastMCP server that demonstrates every major feature of the
framework in a single, cohesive codebase.

---

## Features at a glance

| Feature | Where |
|---|---|
| JWT auth (HMAC / JWKS) | `auth.py` + `config.py` |
| Static dev tokens | `auth.py` → `DEV_TOKENS` |
| Middleware pipeline (8 layers) | `server.py` — `middleware=[...]` |
| Per-tool scope enforcement (tag-driven) | `middleware.py` → `ScopeEnforcementMiddleware` |
| Auth context extraction to session state | `middleware.py` → `AuthContextMiddleware` |
| Structured audit log | `middleware.py` → `AuditLogMiddleware` |
| Rate limiting, timing, response capping | Built-in FastMCP middleware |
| Public tools | `server.py` §2 |
| Bearer-token tools | `server.py` §3–4 |
| Resources (static + template) | `server.py` §5 |
| Multi-message prompts | `server.py` §6 |
| Context (logging, progress, state, transport) | Throughout tool bodies |
| Tag-based visibility (hide experimental) | `mcp.disable(tags={"experimental"})` |
| Custom HTTP routes (health/ready probes) | `server.py` §1 |
| Lifespan (seed / cleanup) | `server.py` — `lifespan()` |
| Error masking in production | `mask_error_details=settings.mask_error_details` |
| Cursor-based pagination | `list_page_size=settings.list_page_size` |

---

## Architecture

```
                  ┌─────────────────────────────────────────────┐
                  │                  MCP Client                  │
                  └───────────────────────┬─────────────────────┘
                                          │  Authorization: Bearer <token>
                  ┌───────────────────────▼─────────────────────┐
                  │            FastMCP HTTP Transport            │
                  │      JWTVerifier / StaticTokenVerifier       │  ← auth.py
                  └───────────────────────┬─────────────────────┘
                                          │  token validated ✓
                  ┌───────────────────────▼─────────────────────┐
                  │            Middleware Pipeline               │
                  │  1. ErrorHandlingMiddleware    (outermost)   │
                  │  2. StructuredLoggingMiddleware              │
                  │  3. TimingMiddleware                         │
                  │  4. SlidingWindowRateLimitingMiddleware      │
                  │  5. AuthContextMiddleware  ← middleware.py   │
                  │       ↳ writes user_id, user_scopes to state │
                  │  6. ScopeEnforcementMiddleware ← middleware  │
                  │       ↳ reads scope:* tags on tool           │
                  │  7. AuditLogMiddleware     ← middleware.py   │
                  │  8. ResponseLimitingMiddleware (innermost)   │
                  └───────────────────────┬─────────────────────┘
                                          │
                  ┌───────────────────────▼─────────────────────┐
                  │               Tool / Resource / Prompt       │
                  │  ctx.get_state("user_id")                    │
                  │  ctx.get_state("user_scopes")                │
                  │  ctx.info / ctx.report_progress              │
                  └─────────────────────────────────────────────┘
```

---

## Quick start

```bash
# 1. Clone and enter the directory
cd demp_fastmcp_server

# 2. Install dependencies
uv sync --extra fastmcp-server

# 3. Configure environment
cp .env.example .env
# Edit .env if you want to change ports, tokens, etc.

# 4. Run in development mode (uses static dev tokens, no JWT infra needed)
uv run server.py
# Server starts at http://0.0.0.0:8000

# 5. Test with the MCP Inspector UI
uv run fastmcp dev server.py
```

---

## Development tokens

When `ENVIRONMENT=development` (default), use these plain bearer tokens:

| Token | Role | Scopes |
|---|---|---|
| `dev-admin-2024` | admin | tasks:read/write/admin, users:read, admin |
| `dev-user-2024` | user | tasks:read/write, users:read |
| `dev-readonly-2024` | viewer | tasks:read |

```bash
# Example: list tasks with the user token
curl -H "Authorization: Bearer dev-user-2024" \
     http://localhost:8000/mcp
```

---

## Production JWT tokens

When `ENVIRONMENT=production` or `ENVIRONMENT=staging`, generate signed JWTs:

```bash
# Admin token
uv run generate_token.py --role admin \
    --scopes tasks:read tasks:write tasks:admin users:read

# Read-only token, 2-hour expiry
uv run generate_token.py --subject alice --email alice@co.com \
    --scopes tasks:read --expires 2
```

For an external IdP (Auth0, WorkOS, Cognito), replace `JWTVerifier` in
`auth.py` with one that uses `jwks_uri=` for automatic key rotation:

```python
return JWTVerifier(
    jwks_uri="https://your-idp.example.com/.well-known/jwks.json",
    issuer="https://your-idp.example.com",
    audience="taskhub-mcp-server",
    required_scopes=["tasks:read"],
)
```

---

## Scope reference

| Scope | Tools |
|---|---|
| `tasks:read` | `list_tasks`, `get_task` |
| `tasks:write` | `create_task`, `update_task` |
| `tasks:admin` | `delete_task` |
| `users:read` | `get_user_profile` |

To protect a new tool, add `"scope:<name>"` to its tags:

```python
@mcp.tool(tags={"my_feature", "scope:tasks:write"})
async def my_new_tool(...):
    ...
```

`ScopeEnforcementMiddleware` picks it up automatically — no guard code needed
inside the tool body.

---

## HTTP endpoints

| Path | Method | Purpose |
|---|---|---|
| `/mcp` | GET / POST | MCP protocol endpoint |
| `/health` | GET | Liveness probe (always 200 if process is up) |
| `/ready` | GET | Readiness probe (503 until data is seeded) |

---

## File layout

```
taskhub_mcp/
├── config.py          Configuration (env vars → Settings dataclass)
├── auth.py            Auth provider factory + JWT generation utility
├── middleware.py      AuthContext, ScopeEnforcement, AuditLog middleware
├── server.py          FastMCP server: tools, resources, prompts, lifespan
├── generate_token.py  CLI: mint signed JWTs for testing
├── requirements.txt
└── .env.example
```

---

## Extending

**Add a new scope-protected tool**
```python
@mcp.tool(tags={"invoices", "scope:invoices:read"})
async def list_invoices(ctx: Context = CurrentContext()) -> list[dict]:
    caller = await ctx.get_state("user_id")
    ...
```

**Add a new resource template**
```python
@mcp.resource("users://{user_id}/tasks")
async def resource_user_tasks(user_id: str) -> list[dict]:
    return [t for t in _tasks.values() if t["assignee"] == user_id]
```

**Switch to Redis session state** (distributed deployments)
```python
from key_value.aio.stores.redis import RedisStore
mcp = FastMCP(..., session_state_store=RedisStore(url="redis://localhost:6379"))
```
