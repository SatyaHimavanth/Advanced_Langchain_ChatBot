"""
chat.py
───────
Streaming chat endpoint.

Conversations (for display) live in the application DB (app_chat_histories /
app_chat_messages). The agent tracks its own state via a per-conversation
thread_id together with the AsyncPostgresStore + checkpointer.

Mapping:
    thread_id  = f"history-{history.id}"     → agent checkpointer / store
    tenant_id  = "default"                    → workspace + memory namespace
    user_id    = str(current_user.id)         → workspace + memory namespace
"""

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from app.db import database, models
from app.db.database import SessionLocal
from app.core import auth
from app.agents.shared.agent_contexts import Context
from app.agents.shared.streaming import (
    InterruptAction,
    build_agent_input,
    stream_agent_sse,
)
from app.settings import settings
from app.logger import get_logger
from app import models_config

logger = get_logger(__name__)

WORKSPACE_ROOT = Path(settings.WORKSPACE_ROOT).resolve()

router = APIRouter(prefix="/chat", tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ChatRequest(BaseModel):
    # Existing conversation to continue; omit to start a new one.
    history_id: int | None = None
    # New user message (omit when resuming from a HITL interrupt).
    message: str | None = None
    # Decision to resume a paused (interrupted) agent run.
    interrupt_action: InterruptAction | None = None
    # Model to use for this request; omit to use the default model.
    model_id: str | None = None


def _check_quota(user: models.User, model_id: str | None) -> None:
    """
    Check if the user has quota remaining. Raises HTTPException if exceeded.
    Free models don't consume quota.
    """
    # Free models don't count against quota
    if model_id and models_config.is_free_model(model_id):
        return
    
    # Unlimited quota
    if user.token_quota == -1:
        return
    
    # Check quota
    if user.tokens_used_this_month >= user.token_quota:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "quota_exceeded",
                "message": "Monthly token quota exceeded. Please contact admin to increase your limit.",
                "used": user.tokens_used_this_month,
                "quota": user.token_quota,
            },
        )


def _reset_monthly_quota_if_needed(user: models.User, db: Session) -> None:
    """Reset quota if we're in a new month (UTC)."""
    now = datetime.now(timezone.utc)
    
    if user.quota_reset_date is None:
        # First time: set reset date to first of current month
        user.quota_reset_date = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        user.tokens_used_this_month = 0
        db.commit()
        return
    
    # Check if we're in a new month
    reset_date = user.quota_reset_date
    if reset_date.tzinfo is None:
        reset_date = reset_date.replace(tzinfo=timezone.utc)
    
    if now.year > reset_date.year or (now.year == reset_date.year and now.month > reset_date.month):
        user.quota_reset_date = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        user.tokens_used_this_month = 0
        db.commit()
        logger.info("Reset monthly quota for user %s", user.username)


@router.post("")
async def chat(
    req: ChatRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent is not ready")

    if not req.message and req.interrupt_action is None:
        raise HTTPException(
            status_code=400,
            detail="Provide a message or an interrupt_action.",
        )

    # ── Validate model selection ────────────────────────────────────────────
    model_id = req.model_id or models_config.get_default_model()
    model_info = models_config.get_model_info(model_id)
    if not model_info or not model_info.enabled:
        raise HTTPException(status_code=400, detail=f"Model '{model_id}' is not available")
    
    # ── Check and reset quota ───────────────────────────────────────────────
    _reset_monthly_quota_if_needed(current_user, db)
    _check_quota(current_user, model_id)
    
    user_id = current_user.id
    is_free_model = models_config.is_free_model(model_id)

    # ── Resolve or create the conversation ──────────────────────────────────
    if req.history_id is not None:
        history = (
            db.query(models.ChatHistory)
            .filter(
                models.ChatHistory.id == req.history_id,
                models.ChatHistory.user_id == current_user.id,
            )
            .first()
        )
        if not history:
            raise HTTPException(status_code=404, detail="Chat history not found")
    else:
        title = (req.message or "New Chat").strip()[:50] or "New Chat"
        history = models.ChatHistory(title=title, user_id=current_user.id)
        db.add(history)
        db.commit()
        db.refresh(history)

    history_id = history.id
    thread_id = history.thread_id

    # ── Persist the user message (skip on pure resume) ──────────────────────
    if req.message and req.interrupt_action is None:
        db.add(
            models.ChatMessage(
                history_id=history_id, role="user", text=req.message
            )
        )
        db.query(models.ChatHistory).filter(
            models.ChatHistory.id == history_id
        ).update({"updated_at": func.now()})
        db.commit()

    # ── Build agent config + context ────────────────────────────────────────
    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": str(current_user.id),
            "tenant_id": "default",
            "model_id": model_id,  # Pass selected model to agent
        }
    }
    context = Context(
        user_name=current_user.username,
        user_id=str(current_user.id),
    )
    agent_input = build_agent_input(
        message=req.message,
        interrupt_action=req.interrupt_action,
    )

    # ── Persist the assistant message once streaming completes ──────────────
    # Uses a fresh session because the request-scoped `db` is closed by the
    # time the stream finishes. ``blocks`` holds the full collapsible timeline
    # (reasoning, tool calls + results, shell, todos, subagents); ``text`` holds
    # the final agent answer shown expanded; ``files`` holds generated artifacts.
    # ``token_usage`` contains the accumulated token counts for the turn.
    def on_complete(
        text: str,
        blocks: list | None = None,
        files: list | None = None,
        token_usage: dict | None = None,
    ) -> None:
        if not text and not blocks and not files:
            return
        
        # Extract token data
        input_tokens = token_usage.get("input_tokens", 0) if token_usage else 0
        output_tokens = token_usage.get("output_tokens", 0) if token_usage else 0
        reasoning_tokens = token_usage.get("reasoning_tokens", 0) if token_usage else 0
        total_tokens = token_usage.get("total_tokens", 0) if token_usage else 0
        model_name = token_usage.get("model_name", model_id) if token_usage else model_id
        
        with SessionLocal() as session:
            # Save the assistant message with token data
            session.add(
                models.ChatMessage(
                    history_id=history_id,
                    role="assistant",
                    text=text or "",
                    blocks=blocks or None,
                    attachments=files or None,
                    model_name=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    reasoning_tokens=reasoning_tokens,
                    total_tokens=total_tokens,
                )
            )
            session.query(models.ChatHistory).filter(
                models.ChatHistory.id == history_id
            ).update({"updated_at": func.now()})
            
            # Update user's quota (only for non-free models)
            if not is_free_model and total_tokens > 0:
                session.query(models.User).filter(
                    models.User.id == user_id
                ).update({
                    "tokens_used_this_month": models.User.tokens_used_this_month + total_tokens
                })
            
            # Update TokenUsage tracking table
            if total_tokens > 0:
                now = datetime.now(timezone.utc)
                year, month = now.year, now.month
                
                token_record = session.query(models.TokenUsage).filter(
                    models.TokenUsage.user_id == user_id,
                    models.TokenUsage.year == year,
                    models.TokenUsage.month == month,
                ).first()
                
                if token_record:
                    token_record.input_tokens += input_tokens
                    token_record.output_tokens += output_tokens
                    token_record.reasoning_tokens += reasoning_tokens
                    token_record.total_tokens += total_tokens
                    token_record.request_count += 1
                else:
                    session.add(models.TokenUsage(
                        user_id=user_id,
                        year=year,
                        month=month,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        reasoning_tokens=reasoning_tokens,
                        total_tokens=total_tokens,
                        request_count=1,
                    ))
            
            session.commit()

    return StreamingResponse(
        stream_agent_sse(
            agent=agent,
            input=agent_input,
            config=config,
            context=context,
            on_complete=on_complete,
        ),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, "X-History-Id": str(history_id)},
    )


def _extract_interrupt_payload(state) -> dict | None:
    """
    Pull a pending HITL interrupt value out of a LangGraph state snapshot,
    tolerating the different shapes across langgraph versions.
    """
    # Top-level interrupts (newer langgraph).
    interrupts = getattr(state, "interrupts", None) or ()
    if not interrupts:
        # Fall back to per-task interrupts.
        collected = []
        for task in getattr(state, "tasks", None) or ():
            collected.extend(getattr(task, "interrupts", None) or ())
        interrupts = tuple(collected)

    if not interrupts:
        return None

    first = interrupts[0]
    value = getattr(first, "value", first)
    return {
        "resumable": getattr(first, "resumable", True),
        "payload": value if isinstance(value, (dict, list)) else str(value),
    }


@router.get("/{history_id}/pending")
async def pending_interrupt(
    history_id: int,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Return whether the agent run for this conversation is paused awaiting a
    human decision, plus the interrupt payload. Lets the UI restore the
    approval prompt after a page reload or chat switch.
    """
    history = (
        db.query(models.ChatHistory)
        .filter(
            models.ChatHistory.id == history_id,
            models.ChatHistory.user_id == current_user.id,
        )
        .first()
    )
    if not history:
        raise HTTPException(status_code=404, detail="Chat history not found")

    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        return {"interrupted": False}

    config = {
        "configurable": {
            "thread_id": history.thread_id,
            "user_id": str(current_user.id),
            "tenant_id": "default",
        }
    }

    try:
        state = await agent.aget_state(config)
    except Exception:
        logger.exception("pending_interrupt: aget_state failed for history %s", history_id)
        return {"interrupted": False}

    payload = _extract_interrupt_payload(state)
    if payload is None:
        return {"interrupted": False}
    return {"interrupted": True, **payload}


# ── File preview / download ────────────────────────────────────────────────

# Media types for inline preview in the browser; everything else downloads.
_INLINE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".json": "application/json", ".html": "text/html; charset=utf-8",
    ".py": "text/plain; charset=utf-8", ".js": "text/plain; charset=utf-8",
    ".ts": "text/plain; charset=utf-8", ".css": "text/plain; charset=utf-8",
}


def _resolve_thread_file(history, user_id: str, rel_path: str) -> Path:
    """
    Resolve a workspace-relative path inside this conversation's thread dir,
    blocking traversal. Layout: WORKSPACE_ROOT/{tenant}/{user}/{thread_id}/...
    """
    base = (WORKSPACE_ROOT / "default" / str(user_id) / history.thread_id).resolve()

    cleaned = str(rel_path).strip().replace("\\", "/")
    for prefix in ("/workspace/", "workspace/"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    cleaned = cleaned.lstrip("/")

    target = (base / cleaned).resolve()
    if base != target and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid file path")
    return target


@router.get("/{history_id}/file")
def get_file(
    history_id: int,
    path: str = Query(..., description="Workspace-relative file path"),
    download: bool = Query(False),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Serve a file the agent generated in this conversation's workspace.
    `?download=1` forces an attachment download; otherwise images/PDF/text
    are served inline for preview.
    """
    history = (
        db.query(models.ChatHistory)
        .filter(
            models.ChatHistory.id == history_id,
            models.ChatHistory.user_id == current_user.id,
        )
        .first()
    )
    if not history:
        raise HTTPException(status_code=404, detail="Chat history not found")

    target = _resolve_thread_file(history, str(current_user.id), path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ext = target.suffix.lower()
    media_type = _INLINE_MEDIA.get(ext, "application/octet-stream")
    disposition = "attachment" if download or ext not in _INLINE_MEDIA else "inline"
    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=target.name,
        content_disposition_type=disposition,
    )


# ── Run a generated code file ──────────────────────────────────────────────

class RunRequest(BaseModel):
    path: str


# Extension → interpreter argv prefix. `python` uses the backend's own Python.
def _interpreter_for(ext: str) -> list[str] | None:
    ext = ext.lower()
    if ext == ".py":
        return [sys.executable]
    if ext in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        return [node] if node else None
    if ext in (".sh", ".bash"):
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        return [bash] if Path(bash).exists() else None
    return None


_RUN_TIMEOUT = 30  # seconds


@router.post("/{history_id}/run")
def run_file(
    history_id: int,
    req: RunRequest,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Execute a code file from this conversation's workspace and return its
    output. Runs with a timeout, in the file's own thread directory, using an
    interpreter chosen by extension (.py/.js/.sh).
    """
    history = (
        db.query(models.ChatHistory)
        .filter(
            models.ChatHistory.id == history_id,
            models.ChatHistory.user_id == current_user.id,
        )
        .first()
    )
    if not history:
        raise HTTPException(status_code=404, detail="Chat history not found")

    target = _resolve_thread_file(history, str(current_user.id), req.path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    interp = _interpreter_for(target.suffix)
    if interp is None:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot run '{target.suffix or 'this file type'}'. Runnable: .py, .js, .sh",
        )

    try:
        proc = subprocess.run(
            [*interp, target.name],
            cwd=str(target.parent),
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT,
        )
        return {
            "stdout": proc.stdout[-20000:],
            "stderr": proc.stderr[-20000:],
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "",
            "exit_code": None,
            "timed_out": True,
        }
    except Exception as exc:
        logger.exception("run_file failed for history %s path %s", history_id, req.path)
        raise HTTPException(status_code=500, detail=str(exc))
