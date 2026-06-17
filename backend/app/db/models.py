"""
models.py
─────────
Application relational models (users, chat histories, chat messages, token usage).

All table names are prefixed with ``app_`` so they never collide with the
tables LangGraph creates for agent persistence:
  • AsyncPostgresStore  → store, store_migrations, ...
  • AsyncPostgresSaver  → checkpoints, checkpoint_blobs, checkpoint_writes,
                          checkpoint_migrations
None of those overlap with app_users / app_chat_histories / app_chat_messages.

Chat *display* data lives here (conversations + messages). The agent itself
tracks state separately via thread_id + AsyncPostgresStore/Saver.
"""

from sqlalchemy import Column, Integer, BigInteger, String, DateTime, ForeignKey, Text, JSON, Boolean, Index, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime, timezone

from app.db.database import Base


class User(Base):
    """
    Application user with role-based access and token quotas.
    
    Roles:
      - pending: Registered but awaiting admin approval
      - user: Standard approved user
      - admin: Full admin access (can manage users, view stats, configure models)
    
    Token quota resets on the 1st of each month (UTC).
    """
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    
    # Role-based access: pending | user | admin
    role = Column(String(20), nullable=False, default="pending")
    # Whether the user account is approved (admins auto-approved)
    is_approved = Column(Boolean, nullable=False, default=False)
    
    # Token quota management (resets monthly on the 1st UTC)
    # -1 means unlimited (for admins or special accounts)
    token_quota = Column(BigInteger, nullable=False, default=100_000)
    tokens_used_this_month = Column(BigInteger, nullable=False, default=0)
    quota_reset_date = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    
    chats = relationship("ChatHistory", back_populates="user", cascade="all, delete-orphan")
    token_usage = relationship("TokenUsage", back_populates="user", cascade="all, delete-orphan")


class PendingInterrupt(Base):
    """
    Persists HITL interrupt state across page refreshes and backend restarts.
    Written when a stream ends with an active interrupt, cleared on resume.
    Only one pending interrupt per conversation at a time (unique on history_id).
    """
    __tablename__ = "app_pending_interrupts"

    id         = Column(Integer, primary_key=True, index=True)
    history_id = Column(Integer, ForeignKey("app_chat_histories.id", ondelete="CASCADE"),
                        unique=True, nullable=False, index=True)
    payload    = Column(Text, nullable=False)   # JSON — raw interrupt value
    resumable  = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    history = relationship("ChatHistory")


class PendingAssistantTurn(Base):
    """
    Persists the latest in-progress assistant turn so the UI can restore it
    after a refresh or chat switch while streaming is still underway.

    Cleared once the final assistant message is written to app_chat_messages.
    Only one in-progress turn exists per conversation.
    """
    __tablename__ = "app_pending_assistant_turns"

    id = Column(Integer, primary_key=True, index=True)
    history_id = Column(
        Integer,
        ForeignKey("app_chat_histories.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    text = Column(Text, nullable=False, default="")
    blocks = Column(JSON, nullable=True)
    attachments = Column(JSON, nullable=True)
    model_name = Column(String(100), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    history = relationship("ChatHistory")


class TokenUsage(Base):
    """
    Monthly token usage tracking per user.
    
    One row per user per month. Allows historical usage analysis and
    admin reporting without scanning all messages.
    """
    __tablename__ = "app_token_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "year", "month", name="uq_app_token_usage_user_month"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("app_users.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)  # 1-12
    
    # Aggregated token counts for the month
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    reasoning_tokens = Column(BigInteger, nullable=False, default=0)
    total_tokens = Column(BigInteger, nullable=False, default=0)
    
    # Request count for the month
    request_count = Column(Integer, nullable=False, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    
    user = relationship("User", back_populates="token_usage")

class ChatHistory(Base):
    __tablename__ = "app_chat_histories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("app_users.id"), nullable=False)
    title = Column(String(255), nullable=False, default="New Chat")
    status = Column(String(50), nullable=False, default="active")  # active, archived, deleted

    # Stable per-conversation key for agent persistence (checkpointer + store).
    # A uuid4 string so it is globally unique across all users — used as the
    # LangGraph thread_id instead of a guessable "history-{id}".
    thread_id = Column(
        String(36),
        unique=True,
        index=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )
    
    # Pinned chats appear at top of sidebar (max 5 per user enforced in API)
    is_pinned = Column(Boolean, nullable=False, default=False)
    pinned_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    user = relationship("User", back_populates="chats")
    messages = relationship("ChatMessage", back_populates="chat", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "app_chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    history_id = Column(Integer, ForeignKey("app_chat_histories.id"), nullable=False)
    role = Column(String(50), nullable=False)  # user, assistant, system
    text = Column(Text, nullable=False)

    # Full streaming timeline for assistant turns: an ordered list of blocks
    # (reasoning text, tool calls + results, shell commands, todo updates,
    # subagent activity, summarization, etc.). `text` holds the final agent
    # answer; `blocks` holds everything else so the UI can replay the turn with
    # collapsible steps. Null for plain user/system messages.
    blocks = Column(JSON, nullable=True)

    # Files generated during this assistant turn (code/docs/images), as a list
    # of {path, name, action, kind}. The UI renders preview/download links.
    attachments = Column(JSON, nullable=True)
    
    # ── Token usage for assistant responses ─────────────────────────────────
    # Only populated for role='assistant'. Shows what model generated this
    # response and how many tokens were consumed.
    model_name = Column(String(100), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)  # For o1/thinking models
    total_tokens = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    chat = relationship("ChatHistory", back_populates="messages")
