"""
history.py
──────────
Chat history CRUD with pagination.

Features:
  - Paginated list of conversations (active/archived)
  - Pinned chats (max 5 per user) shown at top
  - Paginated messages within a conversation
  - Rename, archive, unarchive, delete (soft), pin/unpin
"""

from datetime import datetime
from typing import Any, Generic, List, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func as sql_func, desc, asc
from sqlalchemy.orm import Session

from app.db import database, models
from app.core import auth

router = APIRouter(prefix="/history", tags=["history"])

# ═══════════════════════════════════════════════════════════════════════════════
# Pagination constants
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 200


# ═══════════════════════════════════════════════════════════════════════════════
# Response schemas
# ═══════════════════════════════════════════════════════════════════════════════

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""
    items: List[T]
    total: int
    offset: int
    limit: int
    has_more: bool


class ChatHistoryBase(BaseModel):
    title: str


class ChatHistoryResponse(ChatHistoryBase):
    id: int
    user_id: int
    status: str
    is_pinned: bool = False
    pinned_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChatMessageBase(BaseModel):
    role: str
    text: str


class ChatMessageResponse(ChatMessageBase):
    id: int
    created_at: datetime
    # Full collapsible timeline for assistant turns (reasoning, tool calls +
    # results, shell, todos, subagents). Null for plain user/system messages.
    blocks: Any | None = None
    # Files generated during the turn ({path, name, action, kind}).
    attachments: Any | None = None
    # Token usage (assistant messages only)
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    class Config:
        from_attributes = True


class ChatHistoryDetailResponse(ChatHistoryResponse):
    """Full chat detail with paginated messages."""
    messages: PaginatedResponse[ChatMessageResponse] | List[ChatMessageResponse] = []


class PinnedChatsResponse(BaseModel):
    """Response for pinned chats listing."""
    pinned: List[ChatHistoryResponse]
    max_pins: int = 5
    current_count: int


# ═══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/", response_model=PaginatedResponse[ChatHistoryResponse])
def get_histories(
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Number of items to return"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Get paginated list of active chats.
    
    Pinned chats are returned first (sorted by pinned_at desc),
    followed by unpinned chats (sorted by updated_at desc).
    """
    base_query = db.query(models.ChatHistory).filter(
        models.ChatHistory.user_id == current_user.id,
        models.ChatHistory.status == "active",
    )
    
    total = base_query.count()
    
    # Order: pinned first (by pinned_at desc), then unpinned (by updated_at desc)
    items = base_query.order_by(
        desc(models.ChatHistory.is_pinned),
        desc(models.ChatHistory.pinned_at),
        desc(models.ChatHistory.updated_at),
    ).offset(offset).limit(limit).all()
    
    return PaginatedResponse(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items)) < total,
    )


@router.get("/archived", response_model=PaginatedResponse[ChatHistoryResponse])
def get_archived_histories(
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Get paginated list of archived chats."""
    base_query = db.query(models.ChatHistory).filter(
        models.ChatHistory.user_id == current_user.id,
        models.ChatHistory.status == "archived",
    )
    
    total = base_query.count()
    items = base_query.order_by(desc(models.ChatHistory.updated_at)).offset(offset).limit(limit).all()
    
    return PaginatedResponse(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items)) < total,
    )


@router.get("/pinned", response_model=PinnedChatsResponse)
def get_pinned_chats(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Get all pinned chats for the user (max 5)."""
    pinned = db.query(models.ChatHistory).filter(
        models.ChatHistory.user_id == current_user.id,
        models.ChatHistory.is_pinned == True,
        models.ChatHistory.status == "active",
    ).order_by(desc(models.ChatHistory.pinned_at)).all()
    
    return PinnedChatsResponse(
        pinned=pinned,
        max_pins=5,
        current_count=len(pinned),
    )


@router.post("/", response_model=ChatHistoryResponse)
def create_history(
    history: ChatHistoryBase,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Create a new chat history."""
    new_history = models.ChatHistory(title=history.title, user_id=current_user.id)
    db.add(new_history)
    db.commit()
    db.refresh(new_history)
    return new_history


@router.get("/{history_id}", response_model=ChatHistoryDetailResponse)
def get_history(
    history_id: int,
    message_offset: int = Query(0, ge=0, description="Message offset (0 = most recent)"),
    message_limit: int = Query(DEFAULT_MESSAGE_PAGE_SIZE, ge=1, le=MAX_MESSAGE_PAGE_SIZE),
    oldest_first: bool = Query(False, description="If true, return oldest messages first"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Get chat history with paginated messages.
    
    By default, returns the most recent messages first (for initial load).
    Set oldest_first=true to load older messages when scrolling up.
    """
    history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    # Count total messages
    total_messages = db.query(sql_func.count(models.ChatMessage.id)).filter(
        models.ChatMessage.history_id == history_id
    ).scalar()
    
    # Get paginated messages
    msg_query = db.query(models.ChatMessage).filter(
        models.ChatMessage.history_id == history_id
    )
    
    if oldest_first:
        msg_query = msg_query.order_by(asc(models.ChatMessage.id))
    else:
        msg_query = msg_query.order_by(desc(models.ChatMessage.id))
    
    messages = msg_query.offset(message_offset).limit(message_limit).all()
    
    # If we fetched newest first, reverse to display oldest-to-newest
    if not oldest_first:
        messages = list(reversed(messages))
    
    messages_response = PaginatedResponse(
        items=messages,
        total=total_messages,
        offset=message_offset,
        limit=message_limit,
        has_more=(message_offset + len(messages)) < total_messages,
    )
    
    return ChatHistoryDetailResponse(
        id=history.id,
        user_id=history.user_id,
        title=history.title,
        status=history.status,
        is_pinned=history.is_pinned or False,
        pinned_at=history.pinned_at,
        created_at=history.created_at,
        updated_at=history.updated_at,
        messages=messages_response,
    )


@router.get("/{history_id}/messages", response_model=PaginatedResponse[ChatMessageResponse])
def get_history_messages(
    history_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_MESSAGE_PAGE_SIZE, ge=1, le=MAX_MESSAGE_PAGE_SIZE),
    oldest_first: bool = Query(True, description="If true, return oldest messages first"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Get paginated messages for a chat (for infinite scroll).
    
    Use oldest_first=true when loading older messages (scroll up).
    Use oldest_first=false when loading newer messages (initial load).
    """
    history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    total = db.query(sql_func.count(models.ChatMessage.id)).filter(
        models.ChatMessage.history_id == history_id
    ).scalar()
    
    msg_query = db.query(models.ChatMessage).filter(
        models.ChatMessage.history_id == history_id
    )
    
    if oldest_first:
        msg_query = msg_query.order_by(asc(models.ChatMessage.id))
    else:
        msg_query = msg_query.order_by(desc(models.ChatMessage.id))
    
    messages = msg_query.offset(offset).limit(limit).all()
    
    # Always return in chronological order for display
    if not oldest_first:
        messages = list(reversed(messages))
    
    return PaginatedResponse(
        items=messages,
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + len(messages)) < total,
    )


@router.put("/{history_id}", response_model=ChatHistoryResponse)
def rename_history(
    history_id: int,
    history: ChatHistoryBase,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Rename a chat history."""
    db_history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.title = history.title
    db.commit()
    db.refresh(db_history)
    return db_history


@router.patch("/{history_id}/archive")
def archive_history(
    history_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Archive a chat (also unpins it)."""
    db_history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.status = "archived"
    db_history.is_pinned = False
    db_history.pinned_at = None
    db.commit()
    return {"status": "success"}


@router.patch("/{history_id}/unarchive")
def unarchive_history(
    history_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Unarchive a chat."""
    db_history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.status = "active"
    db.commit()
    return {"status": "success"}


@router.patch("/{history_id}/pin")
def pin_history(
    history_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    Pin a chat to the top of the sidebar.
    
    Maximum 5 pinned chats per user. Returns error if limit exceeded.
    """
    db_history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
        models.ChatHistory.status == "active",
    ).first()
    
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    if db_history.is_pinned:
        return {"status": "already_pinned", "message": "Chat is already pinned"}
    
    # Check pin count
    pin_count = db.query(sql_func.count(models.ChatHistory.id)).filter(
        models.ChatHistory.user_id == current_user.id,
        models.ChatHistory.is_pinned == True,
        models.ChatHistory.status == "active",
    ).scalar()
    
    if pin_count >= 5:
        raise HTTPException(
            status_code=400,
            detail="Maximum 5 pinned chats allowed. Unpin another chat first.",
        )
    
    db_history.is_pinned = True
    db_history.pinned_at = sql_func.now()
    db.commit()
    return {"status": "success", "pinned_count": pin_count + 1}


@router.patch("/{history_id}/unpin")
def unpin_history(
    history_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Unpin a chat from the top of the sidebar."""
    db_history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.is_pinned = False
    db_history.pinned_at = None
    db.commit()
    return {"status": "success"}


@router.delete("/{history_id}")
def delete_history(
    history_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Soft delete a chat history."""
    db_history = db.query(models.ChatHistory).filter(
        models.ChatHistory.id == history_id,
        models.ChatHistory.user_id == current_user.id,
    ).first()
    
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.status = "deleted"
    db_history.is_pinned = False
    db_history.pinned_at = None
    db.commit()
    return {"status": "success"}
