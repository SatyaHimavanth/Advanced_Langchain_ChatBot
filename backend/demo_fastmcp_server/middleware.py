"""
Three custom middleware classes that compose into the pipeline in server.py:

  AuthContextMiddleware     – Decodes bearer tokens → stores claims in session state
  ScopeEnforcementMiddleware – Reads scope:* tags on tools → rejects insufficient callers
  AuditLogMiddleware        – Emits structured audit trail for every tool call

These sit alongside FastMCP's built-in middleware (ErrorHandling, StructuredLogging,
Timing, RateLimiting, ResponseLimiting). See server.py for the full ordered stack.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import jwt as pyjwt

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger("taskhub.server")
audit_logger = logging.getLogger("taskhub.audit")


# ═════════════════════════════════════════════════════════════════════════════
# 1. AuthContextMiddleware
# ═════════════════════════════════════════════════════════════════════════════

class AuthContextMiddleware(Middleware):
    """
    Decodes bearer tokens and stores identity + scopes in MCP session state.

    Runs in the on_request hook — before any tool, resource, or prompt handler
    fires. By the time it runs, FastMCP's own JWTVerifier / StaticTokenVerifier
    has already accepted the token, so we only need to EXTRACT claims here,
    not re-validate them.

    Supports two token formats transparently:
      • Opaque dev tokens  →  static_tokens dict lookup (development)
      • Signed JWTs        →  PyJWT decode (staging / production)

    Session state keys written:
      user_id     (str)      – subject / client_id claim
      user_email  (str)      – email claim
      user_role   (str)      – role claim
      user_scopes (set[str]) – scopes claim as a Python set
    """

    def __init__(
        self,
        secret: str,
        issuer: str,
        audience: str,
        algorithm: str = "HS256",
        static_tokens: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.algorithm = algorithm
        # Opaque-token → claims map used in development mode.
        # Pass DEV_TOKENS from auth.py here so dev and prod paths are unified.
        self.static_tokens: dict[str, dict[str, Any]] = static_tokens or {}

    async def on_request(self, context: MiddlewareContext, call_next):
        """Extract claims from the Authorization header and store in session state."""
        headers = get_http_headers() or {}
        auth_header = (
            headers.get("authorization")
            or headers.get("Authorization")
            or ""
        )

        if auth_header.startswith("Bearer ") and context.fastmcp_context:
            token = auth_header[7:]
            claims = self._extract_claims(token)

            if claims:
                ctx = context.fastmcp_context
                try:
                    # Normalise: StaticTokenVerifier uses "client_id"; JWTs use "sub"
                    uid = claims.get("sub") or claims.get("client_id", "unknown")
                    await ctx.set_state("user_id",     uid)
                    await ctx.set_state("user_email",  claims.get("email", ""))
                    await ctx.set_state("user_role",   claims.get("role", "user"))
                    await ctx.set_state("user_scopes", set(claims.get("scopes", [])))
                except Exception as exc:
                    # State writes can fail transiently (e.g., during init phase).
                    # Log and continue — the tool will simply see empty state.
                    logger.debug("AuthContextMiddleware: state write failed: %s", exc)

        return await call_next(context)

    def _extract_claims(self, token: str) -> dict[str, Any] | None:
        """
        Try static-token lookup first (dev mode), then JWT decode (prod mode).
        Returns None if neither succeeds so the request still reaches the handler
        (FastMCP's own verifier is the gate; we just enrich context here).
        """
        # ── 1. Static opaque token (development) ──────────────────────────────
        if token in self.static_tokens:
            return self.static_tokens[token]

        # ── 2. Signed JWT (staging / production) ──────────────────────────────
        try:
            return pyjwt.decode(
                token,
                self.secret,
                algorithms=[self.algorithm],
                issuer=self.issuer,
                audience=self.audience,
            )
        except pyjwt.ExpiredSignatureError:
            logger.info("AuthContextMiddleware: expired JWT presented")
        except pyjwt.InvalidTokenError as exc:
            logger.debug("AuthContextMiddleware: JWT decode skipped (%s)", exc)
        except Exception as exc:
            logger.warning("AuthContextMiddleware: unexpected error: %s", exc)

        return None


# ═════════════════════════════════════════════════════════════════════════════
# 2. ScopeEnforcementMiddleware
# ═════════════════════════════════════════════════════════════════════════════

class ScopeEnforcementMiddleware(Middleware):
    """
    Declarative, tag-driven scope enforcement for tools.

    Convention: tag a tool with "scope:<scope_name>" to require that scope.

      @mcp.tool(tags={"scope:tasks:write"})   # caller must have tasks:write
      @mcp.tool(tags={"scope:admin"})          # caller must have admin

    Multiple scope tags = ALL are required (logical AND).

    Implementation detail:
      on_call_tool fires AFTER AuthContextMiddleware's on_request has already
      stored scopes in session state, so get_state("user_scopes") is populated.
    """

    SCOPE_PREFIX = "scope:"

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if not context.fastmcp_context:
            return await call_next(context)

        try:
            tool = await context.fastmcp_context.fastmcp.get_tool(
                context.message.name
            )
            required: set[str] = {
                tag[len(self.SCOPE_PREFIX):]
                for tag in (tool.tags or set())
                if tag.startswith(self.SCOPE_PREFIX)
            }

            if required:
                have: set[str] = (
                    await context.fastmcp_context.get_state("user_scopes") or set()
                )
                missing = required - have
                if missing:
                    # ToolError message is shown to the LLM; be explicit and actionable.
                    raise ToolError(
                        f"Access denied. Missing permission(s): "
                        f"{', '.join(sorted(missing))}. "
                        "Contact your administrator to request elevated access."
                    )

        except ToolError:
            raise  # Always propagate to client
        except Exception as exc:
            # Tag lookup / state access errors must never silently block untagged tools.
            logger.warning(
                "ScopeEnforcementMiddleware: non-fatal error for '%s': %s",
                context.message.name,
                exc,
            )

        return await call_next(context)


# ═════════════════════════════════════════════════════════════════════════════
# 3. AuditLogMiddleware
# ═════════════════════════════════════════════════════════════════════════════

class AuditLogMiddleware(Middleware):
    """
    Emits a structured audit-trail entry for every tool invocation.

    Log format (single line, easy to parse with any log aggregator):
      TOOL_CALL | tool=<name> user=<id> status=ok|error duration_ms=<n>

    In production, route the 'taskhub.audit' logger to your SIEM or
    observability platform (Datadog, Splunk, CloudWatch, Loki, etc.).

    Note: This middleware runs AFTER AuthContextMiddleware has stored user_id
    in session state, so the audit entry always contains the caller's identity.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        start = time.monotonic()
        user_id = "unknown"

        # Read user identity populated by AuthContextMiddleware.
        if context.fastmcp_context:
            try:
                user_id = (
                    await context.fastmcp_context.get_state("user_id") or "unknown"
                )
            except Exception:
                pass

        try:
            result = await call_next(context)
            ms = round((time.monotonic() - start) * 1000, 1)
            audit_logger.info(
                "TOOL_CALL | tool=%s user=%s status=ok duration_ms=%s",
                context.message.name,
                user_id,
                ms,
            )
            return result

        except Exception as exc:
            ms = round((time.monotonic() - start) * 1000, 1)
            audit_logger.warning(
                "TOOL_CALL | tool=%s user=%s status=error error_type=%s duration_ms=%s",
                context.message.name,
                user_id,
                type(exc).__name__,
                ms,
            )
            raise  # Always re-raise; never swallow errors in audit middleware
