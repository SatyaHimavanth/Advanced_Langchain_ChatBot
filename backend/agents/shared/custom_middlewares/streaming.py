"""
agent_streaming.py
──────────────────
FastAPI SSE streaming for LangChain / deepagents with full middleware awareness.

Streaming format used here — v1 with subgraphs=True + multiple modes
──────────────────────────────────────────────────────────────────────
With subgraphs=True AND a list of stream modes, each yielded chunk is a
3-tuple: (namespace, mode, data). This is the v1 default confirmed by
the deepagents CLI source.

  namespace : tuple  — () for the main graph, ("task:uuid",) for sub-agents
  mode      : str    — "messages" | "updates" | "custom"
  data      : Any    — payload; format varies by mode (see below)

  messages mode  → data = (AIMessageChunk | ToolMessage, metadata_dict)
  updates  mode  → data = {node_name: state_update}
  custom   mode  → data = arbitrary dict from get_stream_writer()

v2 note (LangGraph >= 1.1):
  Pass version="v2" to astream() for a unified StreamPart dict format:
    {"type": ..., "ns": ..., "data": ...}
  v2 also moves interrupts from the "updates" __interrupt__ key to an
  "interrupts" field on "values" stream parts.  Requires adding "values"
  to stream_mode and changing the chunk-unpacking code below accordingly.
  The v1 approach used here avoids the extra bandwidth of "values" mode.

What gets fixed vs the previous version of this file:
  1. _ToolCallBuffer rewritten with index-based tracking (was keyed by id="",
     causing all non-first chunks to collide on the same empty-string key).
  2. _handle_messages_chunk takes buffer: _ToolCallBuffer directly instead of
     receiving a messy single-entry proxy dict.
  3. "custom" stream mode added to astream() call; _handle_custom_chunk added.
  4. flush_all() deduplication fixed via self._emitted set so already-emitted
     index buffers are never re-emitted at stream end.
  5. Generator updated to pass buffer directly instead of building a proxy dict.

SSE event catalogue (what the UI receives):
  text_delta          streaming token          {delta, agent, is_subagent}
  tool_call_start     tool invoked             {tool, args, tool_call_id, agent, is_subagent}
  tool_result         tool returned            {tool, tool_call_id, content, truncated, agent, is_subagent}
  agent_message       full response + metadata {agent, content, model, tokens, latency_ms, is_subagent}
  transfer            handoff confirmed        {from, to, is_subagent}
  shell_command       bash executing           {command, agent, is_subagent}
  shell_session_start shell ready              {agent}
  interrupt           HITL pause               {thread_id, resumable, payload}
  context_summarizing context compressing      {agent, is_subagent}
  model_fallback      fallback triggered       {agent, is_subagent}
  middleware          other middleware event   {kind, detail, agent, is_subagent}
  error               stream error             {message, thread_id}
  done                stream complete          {thread_id}
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, AsyncGenerator

from fastapi.responses import StreamingResponse
from langchain.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

logger = logging.getLogger(__name__)

# ── Pending HITL interrupt registry ───────────────────────────────────────────
# Maps thread_id → interrupt payload so the /resume endpoint can serve it.
# Replace with Redis in multi-worker / multi-process deployments.
_pending_interrupts: dict[str, dict] = {}

# ── SSE helpers ────────────────────────────────────────────────────────────────

_SSE_HEADERS = {
    "Cache-Control":       "no-cache",
    "Connection":          "keep-alive",
    "X-Accel-Buffering":   "no",   # disable nginx proxy buffering
    "Access-Control-Allow-Origin": "*",  # tighten for production
}


def _sse(event_type: str, data: dict) -> str:
    """Format a single SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


# ── Namespace helpers ─────────────────────────────────────────────────────────
# Namespace is () for the main graph, ("task:uuid", ...) for sub-agents.

def _is_subagent(namespace: tuple) -> bool:
    return len(namespace) > 0


def _agent_label(namespace: tuple, node_name: str) -> str:
    """
    Human-readable agent name for UI display.
    Main graph  → use the LangGraph node name directly.
    Sub-agents  → strip UUIDs from the first namespace segment.
                  e.g. ("task:abc123",) → "task"
    """
    if not namespace:
        return node_name
    return namespace[0].split(":")[0]


# ── Tool call buffer ──────────────────────────────────────────────────────────
# Tool calls stream in as JSON fragments across multiple AIMessageChunks.
#
# FIX vs previous version:
#   Old code keyed buffers by tool_call_id.  Subsequent chunks have id=None
#   which becomes "" — all non-first chunks collapsed onto the same "" key,
#   losing the real id and name.
#
#   New code keys by *index* (always present and stable across chunks for a
#   given tool call).  The real id is stored when it arrives (first chunk only)
#   and a synthetic fallback is used until then.
#
#   Flushing logic:
#     • When tool_call_chunks is non-empty but the index changes → the previous
#       index's call is complete; flush it.
#     • When tool_call_chunks is empty → all remaining buffered calls complete.
#   flush_all() is called at stream end for any calls that were never closed.
#   _emitted tracks which indices have already been yielded to prevent double-emit.

class _ToolCallBuffer:
    """
    Per-(namespace, node) buffer that accumulates streaming tool-call chunks
    into complete (tool_call_id, tool_name, args_dict) triples.
    """

    def __init__(self) -> None:
        # index → {"id": str, "name": str, "args_str": str}
        self._by_index: dict[int, dict] = {}
        # indices already emitted — prevents double-emit in flush_all()
        self._emitted: set[int] = set()

    # ── internal ──────────────────────────────────────────────────────────────

    def _flush_one(self, idx: int) -> tuple[str, str, dict]:
        """Mark index as emitted and return (id, name, args_dict)."""
        self._emitted.add(idx)
        buf = self._by_index[idx]
        try:
            args_dict = json.loads(buf["args_str"]) if buf["args_str"] else {}
        except json.JSONDecodeError:
            args_dict = {"_raw": buf["args_str"]}
        return (buf["id"], buf["name"], args_dict)

    # ── public ────────────────────────────────────────────────────────────────

    def ingest(self, chunk: AIMessageChunk) -> list[tuple[str, str, dict]]:
        """
        Feed one AIMessageChunk.
        Returns a list of newly completed (id, name, args_dict) triples.
        """
        completed: list[tuple[str, str, dict]] = []
        seen_indices: set[int] = set()

        for tc in chunk.tool_call_chunks or []:
            idx  = tc.get("index") if tc.get("index") is not None else 0
            tcid = tc.get("id")   # only on the first chunk for this call
            name = tc.get("name") or ""
            args = tc.get("args") or ""

            seen_indices.add(idx)

            if idx not in self._by_index:
                self._by_index[idx] = {
                    "id":       tcid or f"synthetic_{idx}",
                    "name":     name,
                    "args_str": args,
                }
            else:
                self._by_index[idx]["args_str"] += args
                if name and not self._by_index[idx]["name"]:
                    self._by_index[idx]["name"] = name
                # Replace synthetic id with the real one when it finally arrives
                if tcid and self._by_index[idx]["id"].startswith("synthetic_"):
                    self._by_index[idx]["id"] = tcid

        if chunk.tool_call_chunks:
            # Index changed → indices absent from this chunk are now complete
            for idx in list(self._by_index.keys()):
                if idx not in seen_indices and idx not in self._emitted:
                    completed.append(self._flush_one(idx))
        else:
            # No tool_call_chunks at all → every remaining buffered call is done
            for idx in list(self._by_index.keys()):
                if idx not in self._emitted:
                    completed.append(self._flush_one(idx))

        return completed

    def flush_all(self) -> list[tuple[str, str, dict]]:
        """Force-flush all unemitted calls (called once at stream end)."""
        result = []
        for idx in list(self._by_index.keys()):
            if idx not in self._emitted:
                result.append(self._flush_one(idx))
        return result


# ── Transfer tool helpers ─────────────────────────────────────────────────────

def _is_transfer(tool_name: str) -> bool:
    return (
        tool_name.startswith("transfer_to_")
        or tool_name == "transfer_back_to_supervisor"
    )


def _transfer_target(tool_name: str) -> str:
    if tool_name.startswith("transfer_to_"):
        return tool_name.replace("transfer_to_", "").replace("-", " ").title()
    return "Supervisor"


# ── Tool-call event helper ────────────────────────────────────────────────────

def _tool_call_events(
    completed: list[tuple[str, str, dict]],
    agent: str,
    is_sub: bool,
) -> list[tuple[str, dict]]:
    """Convert completed (id, name, args) triples into SSE event tuples."""
    events: list[tuple[str, dict]] = []
    for tcid, tname, targs in completed:
        if not tname:
            continue
        if _is_transfer(tname):
            events.append(("transfer", {
                "from":       agent,
                "to":         _transfer_target(tname),
                "is_subagent": is_sub,
            }))
        elif tname == "shell":
            events.append(("shell_command", {
                "command":    targs.get("command", ""),
                "agent":      agent,
                "is_subagent": is_sub,
            }))
        else:
            events.append(("tool_call_start", {
                "tool":        tname,
                "args":        targs,
                "tool_call_id": tcid,
                "agent":       agent,
                "is_subagent": is_sub,
            }))
    return events


# ── Messages stream parser ────────────────────────────────────────────────────
# Receives chunks from stream_mode="messages".
# Per the docs: data = (message_chunk, metadata_dict)
#   message_chunk : AIMessageChunk (streaming LLM token or tool-call fragment)
#                 | ToolMessage    (complete tool result)
#   metadata      : {"langgraph_node": str, "langgraph_step": int, "tags": list, ...}

def _handle_messages_chunk(
    chunk: Any,
    meta: dict,
    namespace: tuple,
    buffer: _ToolCallBuffer,   # FIX: direct buffer object, not a proxy dict
) -> list[tuple[str, dict]]:
    """
    Process one (chunk, meta) pair from the 'messages' stream.
    Returns list of (event_type, event_data) tuples.
    """
    events: list[tuple[str, dict]] = []
    node   = meta.get("langgraph_node", "")
    agent  = _agent_label(namespace, node)
    is_sub = _is_subagent(namespace)

    # ── Streaming AI tokens ────────────────────────────────────────────────────
    if isinstance(chunk, AIMessageChunk):

        # Text delta — emit immediately for real-time display
        if chunk.content:
            if isinstance(chunk.content, str):
                events.append(("text_delta", {
                    "delta":       chunk.content,
                    "agent":       agent,
                    "is_subagent": is_sub,
                }))
            elif isinstance(chunk.content, list):
                # Anthropic / content-block format
                for block in chunk.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        events.append(("text_delta", {
                            "delta":       block.get("text", ""),
                            "agent":       agent,
                            "is_subagent": is_sub,
                        }))

        # Tool call chunks — accumulate and emit when complete
        completed = buffer.ingest(chunk)
        events.extend(_tool_call_events(completed, agent, is_sub))

    # ── Tool results ───────────────────────────────────────────────────────────
    elif isinstance(chunk, ToolMessage):
        tname   = chunk.name or ""
        content = chunk.content or ""

        # Transfers are already captured on the AIMessageChunk side; skip here
        if not _is_transfer(tname):
            events.append(("tool_result", {
                "tool":         tname,
                "tool_call_id": chunk.tool_call_id or "",
                "content":      content[:1000],
                "truncated":    len(content) > 1000,
                "agent":        agent,
                "is_subagent":  is_sub,
            }))

    return events


# ── Updates stream parser ─────────────────────────────────────────────────────
# Receives chunks from stream_mode="updates".
# Per the docs: data = {node_name: state_update_dict}
# In v1, HITL interrupts appear here as {"__interrupt__": (Interrupt(...),)}

def _handle_updates_chunk(
    node_name: str,
    update: Any,
    namespace: tuple,
    thread_id: str,
) -> list[tuple[str, dict]]:
    """
    Process one node update from the 'updates' stream.
    Returns list of (event_type, event_data) tuples.
    """
    events: list[tuple[str, dict]] = []
    agent  = _agent_label(namespace, node_name)
    is_sub = _is_subagent(namespace)

    # ── HITL interrupt (v1 location: __interrupt__ key in updates data) ────────
    if node_name == "__interrupt__":
        interrupts = update if isinstance(update, (list, tuple)) else [update]
        for interrupt in interrupts:
            value   = getattr(interrupt, "value", interrupt)
            payload = {
                "thread_id": thread_id,
                "resumable": getattr(interrupt, "resumable", True),
                "payload":   value if isinstance(value, dict) else str(value),
            }
            _pending_interrupts[thread_id] = payload
            logger.info("HITL interrupt stored for thread %s", thread_id)
            events.append(("interrupt", payload))
        return events

    # Skip None middleware heartbeat ticks — they carry no information
    if update is None:
        return events

    # ── Middleware lifecycle events ────────────────────────────────────────────
    if "Middleware" in node_name or "middleware" in node_name:

        if "ShellTool" in node_name:
            events.append(("shell_session_start", {"agent": agent}))

        elif "Summarization" in node_name:
            events.append(("context_summarizing", {
                "agent":       agent,
                "is_subagent": is_sub,
            }))

        elif "PII" in node_name:
            pii_type = (
                node_name.split("[")[1].rstrip("]")
                if "[" in node_name else "unknown"
            )
            events.append(("middleware", {
                "kind":        "pii",
                "detail":      pii_type,
                "agent":       agent,
                "is_subagent": is_sub,
            }))

        elif "Fallback" in node_name or "fallback" in node_name:
            events.append(("model_fallback", {
                "agent":       agent,
                "is_subagent": is_sub,
            }))

        elif "TodoList" in node_name:
            if isinstance(update, dict) and update.get("todos"):
                events.append(("middleware", {
                    "kind":        "todos",
                    "detail":      update["todos"],
                    "agent":       agent,
                    "is_subagent": is_sub,
                }))

        # All other middleware ticks are silently skipped
        return events

    # ── Final agent messages (for token/latency metadata) ─────────────────────
    # Text was already streamed token-by-token via messages mode.
    # We emit agent_message here only for its metadata (model, token counts, etc.)
    # which isn't available in the streaming chunks.
    if not isinstance(update, dict):
        return events

    for msg in update.get("messages", []):
        if not isinstance(msg, AIMessage):
            continue
        if msg.additional_kwargs.get("__is_handoff_back"):
            continue
        if msg.content and not msg.tool_calls:
            rmeta   = msg.response_metadata or {}
            usage   = rmeta.get("token_usage", {})
            latency = rmeta.get("latency_checkpoint", {})
            events.append(("agent_message", {
                "agent":   agent,
                "content": msg.content,
                "model":   rmeta.get("model_name", ""),
                "tokens": {
                    "input":  usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                    "total":  usage.get("total_tokens", 0),
                    "cached": usage.get("prompt_tokens_details", {})
                                   .get("cached_tokens", 0),
                },
                "latency_ms": {
                    "ttft":  latency.get("user_visible_ttft_ms", 0),
                    "total": latency.get("total_duration_ms", 0),
                },
                "finish_reason": rmeta.get("finish_reason", ""),
                "is_subagent":   is_sub,
            }))

    return events


# ── Custom stream parser ──────────────────────────────────────────────────────
# Receives chunks from stream_mode="custom".
# Per the docs: data = arbitrary dict emitted via get_stream_writer() inside
# a node or tool.  Deepagents middleware uses this for progress / summarization
# notifications.

def _handle_custom_chunk(
    data: Any,
    namespace: tuple,
) -> list[tuple[str, dict]]:
    """
    Process one payload from the 'custom' stream.
    Returns list of (event_type, event_data) tuples.
    """
    events: list[tuple[str, dict]] = []
    agent  = _agent_label(namespace, "")
    is_sub = _is_subagent(namespace)

    if not isinstance(data, dict):
        return events

    dtype = (data.get("type") or "").lower()

    if "summariz" in dtype:
        events.append(("context_summarizing", {
            "agent":       agent,
            "is_subagent": is_sub,
        }))
    elif dtype == "progress":
        events.append(("middleware", {
            "kind":        "progress",
            "detail":      data.get("data", ""),
            "agent":       agent,
            "is_subagent": is_sub,
        }))
    elif dtype == "pii":
        events.append(("middleware", {
            "kind":        "pii",
            "detail":      data.get("pii_type", "unknown"),
            "agent":       agent,
            "is_subagent": is_sub,
        }))
    elif dtype == "fallback":
        events.append(("model_fallback", {
            "agent":       agent,
            "is_subagent": is_sub,
        }))
    elif dtype:
        # Unknown custom event — pass it through generically
        events.append(("middleware", {
            "kind":        "custom",
            "detail":      data,
            "agent":       agent,
            "is_subagent": is_sub,
        }))

    return events


# ── Core async generator ──────────────────────────────────────────────────────

async def _agent_generator(
    agent,
    stream_input: dict | Command,
    config: dict,
    context: Any | None,
) -> AsyncGenerator[str, None]:
    """
    Core generator. Yields SSE-formatted strings.
    Handles both new conversations (dict input) and HITL resumes (Command input).

    Stream format: v1 with subgraphs=True + list of modes.
    Each chunk is a 3-tuple: (namespace, mode, data)

      namespace : tuple  — () main graph, ("task:uuid",) sub-agent
      mode      : str    — "messages" | "updates" | "custom"
      data      :        — (message_chunk, meta) | {node: update} | custom_dict
    """
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")
    kwargs: dict = {}
    if context is not None:
        kwargs["context"] = context

    # One _ToolCallBuffer per (namespace, node_name) pair — prevents sub-agent
    # tool calls from colliding with main-agent ones.
    tool_buffers: defaultdict = defaultdict(_ToolCallBuffer)

    try:
        async for chunk in agent.astream(
            input=stream_input,
            config=config,
            stream_mode=["messages", "updates", "custom"],
            subgraphs=True,
            **kwargs,
        ):
            # With subgraphs=True + list of stream modes → 3-tuple
            if not isinstance(chunk, tuple) or len(chunk) != 3:
                continue

            namespace, mode, data = chunk

            # ── Real-time tokens + tool call streaming ─────────────────────────
            if mode == "messages":
                # data = (message_chunk, metadata_dict)
                if not isinstance(data, tuple) or len(data) != 2:
                    continue
                msg_chunk, meta = data
                node    = meta.get("langgraph_node", "")
                buf_key = (namespace, node)
                for evt_type, evt_data in _handle_messages_chunk(
                    msg_chunk, meta, namespace, tool_buffers[buf_key]   # FIX: pass buffer directly
                ):
                    yield _sse(evt_type, evt_data)

            # ── State updates: interrupts, middleware, final messages ───────────
            elif mode == "updates":
                # data = {node_name: state_update}
                if not isinstance(data, dict):
                    continue
                for node_name, update in data.items():
                    for evt_type, evt_data in _handle_updates_chunk(
                        node_name, update, namespace, thread_id
                    ):
                        yield _sse(evt_type, evt_data)

            # ── Custom middleware / tool events ────────────────────────────────
            elif mode == "custom":
                # data = arbitrary dict from get_stream_writer()
                for evt_type, evt_data in _handle_custom_chunk(data, namespace):
                    yield _sse(evt_type, evt_data)

        # ── Force-flush any tool calls that were never closed by a content chunk
        for (ns_key, node_name), buf in tool_buffers.items():
            agent_label = _agent_label(ns_key, node_name)
            is_sub      = _is_subagent(ns_key)
            for evt_type, evt_data in _tool_call_events(
                buf.flush_all(), agent_label, is_sub
            ):
                yield _sse(evt_type, evt_data)

    except Exception as exc:
        logger.exception("Stream error for thread %s: %s", thread_id, exc)
        yield _sse("error", {"message": str(exc), "thread_id": thread_id})

    finally:
        yield _sse("done", {"thread_id": thread_id})


# ── Public API ────────────────────────────────────────────────────────────────

def stream_agent_response(
    agent,
    user_message: str,
    config: dict,
    context: Any | None = None,
) -> StreamingResponse:
    """
    Start a new agent conversation and stream the response as SSE.

    Args:
        agent:        Compiled supervisor agent (create_agent / create_deep_agent).
        user_message: The user's text input.
        config:       LangGraph config. Must contain thread_id.
                      {"configurable": {"thread_id": "some-uuid"}}
        context:      Optional context dataclass for agents with context_schema.
                      e.g. Context(user_name="Alice")

    Returns:
        FastAPI StreamingResponse (text/event-stream).

    FastAPI usage:
        @app.post("/chat")
        async def chat(req: ChatRequest):
            config = {"configurable": {"thread_id": req.thread_id or str(uuid4())}}
            return stream_agent_response(
                agent=supervisor_agent,
                user_message=req.message,
                config=config,
                context=Context(user_name=req.user_name),
            )
    """
    return StreamingResponse(
        _agent_generator(
            agent=agent,
            stream_input={"messages": [HumanMessage(content=user_message)]},
            config=config,
            context=context,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def resume_agent_stream(
    agent,
    thread_id: str,
    decisions: list[dict],
    context: Any | None = None,
    config: dict | None = None,
) -> StreamingResponse:
    """
    Resume an agent that was paused by HumanInTheLoopMiddleware.

    Args:
        agent:      Same compiled agent as the original call.
        thread_id:  Must match the thread_id of the interrupted conversation.
        decisions:  List of decision dicts — one per interrupted tool call:
                      {"type": "approve"}
                      {"type": "reject", "message": "reason for rejection"}
                      {"type": "edit",   "edited_action": {"name": "tool", "args": {...}}}
        context:    Same context as the original call.
        config:     Optional override. Defaults to thread_id-only configurable.

    Returns:
        FastAPI StreamingResponse continuing the interrupted stream.

    FastAPI usage:
        @app.post("/chat/resume")
        async def resume(req: ResumeRequest):
            return resume_agent_stream(
                agent=supervisor_agent,
                thread_id=req.thread_id,
                decisions=req.decisions,
                context=Context(user_name=req.user_name),
            )
    """
    if config is None:
        config = {"configurable": {"thread_id": thread_id}}

    _pending_interrupts.pop(thread_id, None)   # clear stored interrupt

    return StreamingResponse(
        _agent_generator(
            agent=agent,
            stream_input=Command(resume={"decisions": decisions}),
            config=config,
            context=context,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def get_pending_interrupt(thread_id: str) -> dict | None:
    """
    Return the pending HITL interrupt payload for a thread, or None.
    Useful for polling or reconnect logic when the SSE connection drops
    mid-interrupt.
    """
    return _pending_interrupts.get(thread_id)


# ── FastAPI route wiring example ──────────────────────────────────────────────
#
# from uuid import uuid4
# from fastapi import FastAPI
# from pydantic import BaseModel
# from agents.supervisor_agent.agent import supervisor_agent, Context
# from agent_streaming import stream_agent_response, resume_agent_stream, get_pending_interrupt
#
# app = FastAPI()
#
#
# class ChatRequest(BaseModel):
#     message:   str
#     thread_id: str | None = None
#     user_name: str = "User"
#
#
# class ResumeRequest(BaseModel):
#     thread_id: str
#     user_name: str = "User"
#     decisions: list[dict]
#
#
# @app.post("/chat")
# async def chat(req: ChatRequest):
#     config  = {"configurable": {"thread_id": req.thread_id or str(uuid4())}}
#     context = Context(user_name=req.user_name)
#     return stream_agent_response(supervisor_agent, req.message, config, context)
#
#
# @app.post("/chat/resume")
# async def resume(req: ResumeRequest):
#     context = Context(user_name=req.user_name)
#     return resume_agent_stream(supervisor_agent, req.thread_id, req.decisions, context)
#
#
# @app.get("/chat/interrupt/{thread_id}")
# async def check_interrupt(thread_id: str):
#     """Polling fallback: check for pending HITL interrupt after SSE reconnect."""
#     return get_pending_interrupt(thread_id)
#
#
# ── SSE event handling on the frontend (JavaScript) ─────────────────────────
#
# const es = new EventSource("/chat");
#
# es.addEventListener("text_delta",          e => appendToken(JSON.parse(e.data)));
# es.addEventListener("tool_call_start",     e => showToolCall(JSON.parse(e.data)));
# es.addEventListener("tool_result",         e => showToolResult(JSON.parse(e.data)));
# es.addEventListener("agent_message",       e => finaliseMessage(JSON.parse(e.data)));
# es.addEventListener("transfer",            e => showHandoff(JSON.parse(e.data)));
# es.addEventListener("shell_command",       e => showShellCmd(JSON.parse(e.data)));
# es.addEventListener("shell_session_start", e => showShellReady(JSON.parse(e.data)));
# es.addEventListener("interrupt",           e => showHITLDialog(JSON.parse(e.data)));
# es.addEventListener("context_summarizing", e => showSummarizingSpinner(JSON.parse(e.data)));
# es.addEventListener("model_fallback",      e => showFallbackBadge(JSON.parse(e.data)));
# es.addEventListener("middleware",          e => handleMiddleware(JSON.parse(e.data)));
# es.addEventListener("error",              e => showError(JSON.parse(e.data)));
# es.addEventListener("done",               e => { es.close(); onComplete(); });