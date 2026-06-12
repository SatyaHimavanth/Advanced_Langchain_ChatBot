"""
user_memory_middleware.py
─────────────────────────
Long-term, per-user memory for the agent, backed by the same LangGraph Store
that powers the `/memory/` route (AsyncPostgresStore in production).

Two capabilities:

  1. A ``remember`` tool the agent calls to persist a durable fact or
     preference the user shares (e.g. "I prefer Python").

  2. Automatic recall: at the start of every model call, the user's saved
     memories are read from the store and injected into the system prompt, so
     the agent always "knows" them without having to call a tool first.

Storage layout (separate from the FilesystemMiddleware `/memory/` namespace to
avoid clashing with its internal file format):

    namespace = ("memories", tenant_id, user_id)
    key       = "profile"
    value     = {"notes": ["...", "..."]}
"""

from __future__ import annotations

import importlib
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore

import logging

logger = logging.getLogger(__name__)

# ── get_config (version-defensive, same pattern as the other middlewares) ────
_get_config_impl: Any = None
for _mod, _fn in [
    ("langgraph.config", "get_config"),
    ("langgraph.pregel", "get_config"),
    ("langgraph.runtime", "get_config"),
]:
    try:
        _get_config_impl = getattr(importlib.import_module(_mod), _fn)
        break
    except (ImportError, AttributeError):
        continue


def _get_config() -> dict:
    if _get_config_impl is None:
        return {}
    try:
        result = _get_config_impl()
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _runtime_config(runtime: Any) -> dict:
    cfg = getattr(runtime, "config", None)
    if isinstance(cfg, dict):
        return cfg
    return _get_config()


class UserMemoryMiddleware(AgentMiddleware):
    """Persist + recall per-user long-term memory via the LangGraph Store."""

    _KEY = "profile"

    def __init__(
        self,
        store: BaseStore,
        *,
        tenant_id_key: str = "tenant_id",
        user_id_key: str = "user_id",
        default_tenant: str = "default",
        default_user: str = "default_user",
        max_notes: int = 200,
    ) -> None:
        super().__init__()
        self._store = store
        self._tenant_key = tenant_id_key
        self._user_key = user_id_key
        self._default_tenant = default_tenant
        self._default_user = default_user
        self._max_notes = max_notes

        # Register the `remember` tool with the agent (closures over self).
        remember_async = self._remember_async

        @tool("remember")
        async def remember(note: str, config: RunnableConfig) -> str:
            """Save a durable fact or preference about the USER to long-term
            memory so it is available in future conversations. Call this
            whenever the user shares a lasting preference, personal detail, or
            instruction they want remembered (e.g. preferred languages, tools,
            coding style, name). Do NOT use it for transient task details.

            Args:
                note: A concise, durable fact or preference about the user.
            """
            # `config` is injected by LangChain (not shown to the LLM); it
            # carries the same configurable (tenant_id/user_id) the rest of the
            # graph uses, so saves and recalls share one namespace.
            return await remember_async(note, config)

        self.tools = [remember]

    # ── namespace resolution ─────────────────────────────────────────────────
    # Save (tool) and recall (model hook) MUST resolve to the same namespace.
    # We derive (tenant, user) from whichever sources are available, with the
    # same precedence + defaults on both paths:
    #   user  : config.configurable.user_id  →  runtime.context.user_id  → default
    #   tenant: config.configurable.tenant_id →  default ("default", matching the
    #           chat router) — Context carries no tenant.

    def _resolve(self, *, config: dict | None = None, runtime: Any = None) -> tuple[str, str, str]:
        cfg = config if isinstance(config, dict) else _get_config()
        c = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}

        ctx = getattr(runtime, "context", None) if runtime is not None else None

        def _from_ctx(attr: str):
            if ctx is None:
                return None
            if isinstance(ctx, dict):
                return ctx.get(attr)
            return getattr(ctx, attr, None)

        tenant = str(c.get(self._tenant_key) or _from_ctx("tenant_id") or self._default_tenant)
        user = str(c.get(self._user_key) or _from_ctx("user_id") or self._default_user)
        return ("memories", tenant, user)

    # ── store helpers ────────────────────────────────────────────────────────

    async def _aload_notes(self, ns: tuple[str, str, str]) -> list[str]:
        try:
            item = await self._store.aget(ns, self._KEY)
        except Exception:
            logger.exception("UserMemoryMiddleware: failed to read memory")
            return []
        if not item:
            return []
        value = getattr(item, "value", item)
        notes = value.get("notes") if isinstance(value, dict) else None
        return [str(n) for n in notes] if isinstance(notes, list) else []

    async def _remember_async(self, note: str, config: dict | None = None) -> str:
        ns = self._resolve(config=config)
        notes = await self._aload_notes(ns)
        clean = note.strip()
        if clean and clean not in notes:
            notes.append(clean)
            notes = notes[-self._max_notes:]
            try:
                await self._store.aput(ns, self._KEY, {"notes": notes})
            except Exception:
                logger.exception("UserMemoryMiddleware: failed to save memory")
                return "I couldn't save that to memory right now."
        logger.info("UserMemoryMiddleware: saved note to namespace %s", ns)
        return f"Saved to long-term memory: {clean}"

    # ── prompt injection (auto-recall) ──────────────────────────────────────────

    def _prompt(self, notes: list[str]) -> str:
        if not notes:
            return (
                "\n\n## Long-term memory\n"
                "You have no saved notes about this user yet. When the user "
                "shares a durable preference or personal detail (and especially "
                "when they ask you to 'remember' something), call the `remember` "
                "tool to persist it."
            )
        bullet = "\n".join(f"- {n}" for n in notes)
        return (
            "\n\n## What you remember about this user\n"
            f"{bullet}\n"
            "Honor these preferences when generating responses and code. If the "
            "user shares a new durable preference, save it with the `remember` tool."
        )

    # ── prompt injection (auto-recall) ────────────────────────────────────────
    # Implemented via wrap_model_call so we can override the request's
    # system_message — `before_model` returning a state dict has no key for
    # "system prompt suffix", so we have to mutate the model request directly.

    def _augmented_system(self, request: Any, notes: list[str]) -> SystemMessage:
        existing = ""
        sm = getattr(request, "system_message", None)
        if isinstance(sm, SystemMessage):
            content = sm.content
            if isinstance(content, str):
                existing = content
            elif isinstance(content, list):
                # Some providers use content blocks; serialize text parts.
                existing = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        return SystemMessage(content=existing + self._prompt(notes))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        ns = self._resolve(runtime=getattr(request, "runtime", None))
        notes = await self._aload_notes(ns)
        logger.info("UserMemoryMiddleware: recalled %d note(s) from %s", len(notes), ns)
        new_request = request.override(system_message=self._augmented_system(request, notes))
        return await handler(new_request)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        ns = self._resolve(runtime=getattr(request, "runtime", None))
        try:
            item = self._store.get(ns, self._KEY)
            value = getattr(item, "value", item) if item else None
            notes = value.get("notes") if isinstance(value, dict) else []
            notes = [str(n) for n in notes] if isinstance(notes, list) else []
        except Exception:
            notes = []
        logger.info("UserMemoryMiddleware (sync): recalled %d note(s) from %s", len(notes), ns)
        new_request = request.override(system_message=self._augmented_system(request, notes))
        return handler(new_request)
