"""
admin.py
────────
Admin-only API endpoints.

Features:
  - User management (list, approve/reject, create, update roles/quotas)
  - Usage statistics (token consumption per user, overall stats)
  - Model configuration (CRUD from YAML)
  - System health and metrics

All endpoints require admin role. Default admin created from ADMIN_USERNAME/
ADMIN_PASSWORD in .env on first startup.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Generic, List, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func as sql_func, desc, extract
from sqlalchemy.orm import Session

from app.db import database, models
from app.core import auth
from app.settings import settings
from app import models_config
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ═══════════════════════════════════════════════════════════════════════════════
# Admin authentication dependency
# ═══════════════════════════════════════════════════════════════════════════════

def require_admin(
    current_user: models.User = Depends(auth.get_current_user),
) -> models.User:
    """Dependency that requires the current user to be an admin."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access required",
        )
    return current_user


# ═══════════════════════════════════════════════════════════════════════════════
# Pagination
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""
    items: List[T]
    total: int
    offset: int
    limit: int
    has_more: bool


# ═══════════════════════════════════════════════════════════════════════════════
# User management schemas
# ═══════════════════════════════════════════════════════════════════════════════

class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_approved: bool
    token_quota: int
    tokens_used_this_month: int
    quota_reset_date: datetime | None
    created_at: datetime
    updated_at: datetime | None
    # Computed fields
    chat_count: int = 0
    message_count: int = 0

    class Config:
        from_attributes = True


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    role: str = Field("user", pattern="^(user|admin|disabled)$")
    is_approved: bool = True
    token_quota: int = Field(default_factory=lambda: settings.DEFAULT_TOKEN_QUOTA)


class UserUpdateRequest(BaseModel):
    role: str | None = Field(None, pattern="^(pending|user|admin|disabled)$")
    is_approved: bool | None = None
    token_quota: int | None = Field(None, ge=-1)  # -1 = unlimited
    reset_usage: bool = False  # If true, reset tokens_used_this_month to 0


class QuotaIncreaseRequest(BaseModel):
    additional_tokens: int = Field(..., gt=0)


class UserApprovalRequest(BaseModel):
    role: str = Field("user", pattern="^(user|admin)$")


# ═══════════════════════════════════════════════════════════════════════════════
# Stats schemas
# ═══════════════════════════════════════════════════════════════════════════════

class UserStatsResponse(BaseModel):
    user_id: int
    username: str
    role: str
    chat_count: int
    message_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tokens: int
    current_month_tokens: int
    token_quota: int
    quota_remaining: int  # -1 if unlimited


class OverallStatsResponse(BaseModel):
    total_users: int
    pending_users: int
    approved_users: int
    admin_users: int
    total_chats: int
    total_messages: int
    total_tokens_this_month: int
    active_users_this_month: int  # Users who sent at least one message


class MonthlyUsageResponse(BaseModel):
    year: int
    month: int
    user_id: int
    username: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    request_count: int


# ═══════════════════════════════════════════════════════════════════════════════
# Model config schemas
# ═══════════════════════════════════════════════════════════════════════════════

class ModelConfigResponse(BaseModel):
    default_model: str
    tiers: dict[str, List[str]]
    models: dict[str, Any]


class ModelConfigUpdateRequest(BaseModel):
    default_model: str | None = None
    tiers: dict[str, List[str]] | None = None
    models: dict[str, Any] | None = None


class ModelEntryRequest(BaseModel):
    name: str
    provider: str
    model: str | None = None
    deployment: str | None = None
    azure_deployment: str | None = None
    endpoint: str | None = None
    azure_endpoint: str | None = None
    base_url: str | None = None
    api_version: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    organization: str | None = None
    temperature: float | None = None
    description: str = ""
    context_window: int = 128000
    max_output: int = 16384
    supports_reasoning: bool = False
    supports_vision: bool = False
    enabled: bool = True
    is_free: bool = False


def _clear_agent_cache(request: Request) -> None:
    """Clear cached per-model agents after model config changes."""
    if hasattr(request.app.state, "agent_cache"):
        request.app.state.agent_cache = {}


# ═══════════════════════════════════════════════════════════════════════════════
# User management endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/users", response_model=PaginatedResponse[UserResponse])
def list_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    role: str | None = Query(None, description="Filter by role: pending|user|admin"),
    approved: bool | None = Query(None, description="Filter by approval status"),
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """List all users with pagination and optional filters."""
    query = db.query(models.User)

    if role:
        query = query.filter(models.User.role == role)
    if approved is not None:
        query = query.filter(models.User.is_approved == approved)

    total = query.count()
    users = query.order_by(desc(models.User.created_at)).offset(offset).limit(limit).all()

    if not users:
        return PaginatedResponse(items=[], total=total, offset=offset, limit=limit, has_more=False)

    user_ids = [u.id for u in users]

    # Two aggregate queries instead of 2×N individual ones.
    chat_counts = dict(
        db.query(models.ChatHistory.user_id, sql_func.count(models.ChatHistory.id))
        .filter(models.ChatHistory.user_id.in_(user_ids))
        .group_by(models.ChatHistory.user_id)
        .all()
    )
    msg_counts = dict(
        db.query(models.ChatHistory.user_id, sql_func.count(models.ChatMessage.id))
        .join(models.ChatMessage, models.ChatMessage.history_id == models.ChatHistory.id)
        .filter(models.ChatHistory.user_id.in_(user_ids))
        .group_by(models.ChatHistory.user_id)
        .all()
    )

    result = [
        UserResponse(
            id=u.id,
            username=u.username,
            role=u.role,
            is_approved=u.is_approved,
            token_quota=u.token_quota,
            tokens_used_this_month=u.tokens_used_this_month,
            quota_reset_date=u.quota_reset_date,
            created_at=u.created_at,
            updated_at=u.updated_at,
            chat_count=chat_counts.get(u.id, 0),
            message_count=msg_counts.get(u.id, 0),
        )
        for u in users
    ]

    return PaginatedResponse(
        items=result,
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + len(result)) < total,
    )


@router.get("/users/pending", response_model=List[UserResponse])
def list_pending_users(
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """List all users awaiting approval. Auto-rejects expired pending users."""
    # Auto-reject expired pending users if configured
    expire_days = settings.PENDING_USER_EXPIRE_DAYS
    if expire_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=expire_days)
        expired_users = db.query(models.User).filter(
            models.User.is_approved == False,
            models.User.role == "pending",
            models.User.created_at < cutoff,
        ).all()
        
        for user in expired_users:
            logger.info("Auto-rejecting expired pending user: %s (registered %s)", 
                       user.username, user.created_at)
            db.delete(user)
        
        if expired_users:
            db.commit()
    
    # Return remaining pending users
    users = db.query(models.User).filter(
        models.User.is_approved == False,
        models.User.role == "pending",
    ).order_by(desc(models.User.created_at)).all()
    
    return [UserResponse(
        id=u.id,
        username=u.username,
        role=u.role,
        is_approved=u.is_approved,
        token_quota=u.token_quota,
        tokens_used_this_month=u.tokens_used_this_month,
        quota_reset_date=u.quota_reset_date,
        created_at=u.created_at,
        updated_at=u.updated_at,
        chat_count=0,
        message_count=0,
    ) for u in users]


@router.post("/users", response_model=UserResponse)
def create_user(
    req: UserCreateRequest,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Create a new user (admin-created users are auto-approved)."""
    existing = db.query(models.User).filter(
        models.User.username == req.username
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    
    hashed_password = auth.get_password_hash(req.password)
    new_user = models.User(
        username=req.username,
        hashed_password=hashed_password,
        role=req.role,
        is_approved=False if req.role == "disabled" else req.is_approved,
        token_quota=-1 if req.role == "admin" else req.token_quota,
        tokens_used_this_month=0,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    logger.info("Admin %s created user %s with role %s", admin.username, req.username, req.role)
    
    return UserResponse(
        id=new_user.id,
        username=new_user.username,
        role=new_user.role,
        is_approved=new_user.is_approved,
        token_quota=new_user.token_quota,
        tokens_used_this_month=new_user.tokens_used_this_month,
        quota_reset_date=new_user.quota_reset_date,
        created_at=new_user.created_at,
        updated_at=new_user.updated_at,
        chat_count=0,
        message_count=0,
    )


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Get a specific user's details."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    chat_count = db.query(sql_func.count(models.ChatHistory.id)).filter(
        models.ChatHistory.user_id == user.id
    ).scalar()
    message_count = db.query(sql_func.count(models.ChatMessage.id)).join(
        models.ChatHistory
    ).filter(models.ChatHistory.user_id == user.id).scalar()
    
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        is_approved=user.is_approved,
        token_quota=user.token_quota,
        tokens_used_this_month=user.tokens_used_this_month,
        quota_reset_date=user.quota_reset_date,
        created_at=user.created_at,
        updated_at=user.updated_at,
        chat_count=chat_count,
        message_count=message_count,
    )


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    req: UserUpdateRequest,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Update a user's role, approval status, or quota."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    previous_role = user.role
    if req.role is not None:
        user.role = req.role
        if req.role in {"user", "admin"}:
            user.is_approved = True
        elif req.role in {"pending", "disabled"}:
            user.is_approved = False
        if req.role == "admin":
            user.token_quota = -1
        elif previous_role == "admin" and req.role == "user" and user.token_quota == -1:
            user.token_quota = settings.DEFAULT_TOKEN_QUOTA
    if req.is_approved is not None:
        user.is_approved = req.is_approved
        # When approving, also set role to 'user' if still 'pending'
        if req.is_approved and user.role == "pending":
            user.role = "user"
        if not req.is_approved and user.role in {"user", "admin"}:
            user.role = "pending"
    if req.token_quota is not None:
        user.token_quota = req.token_quota
    if req.reset_usage:
        user.tokens_used_this_month = 0
        user.quota_reset_date = datetime.now(timezone.utc)
    if user.role == "disabled":
        user.is_approved = False
    elif user.role == "admin":
        user.is_approved = True
        user.token_quota = -1
    
    db.commit()
    db.refresh(user)
    
    logger.info("Admin %s updated user %s: role=%s, approved=%s, quota=%s",
                admin.username, user.username, user.role, user.is_approved, user.token_quota)
    
    chat_count = db.query(sql_func.count(models.ChatHistory.id)).filter(
        models.ChatHistory.user_id == user.id
    ).scalar()
    message_count = db.query(sql_func.count(models.ChatMessage.id)).join(
        models.ChatHistory
    ).filter(models.ChatHistory.user_id == user.id).scalar()
    
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        is_approved=user.is_approved,
        token_quota=user.token_quota,
        tokens_used_this_month=user.tokens_used_this_month,
        quota_reset_date=user.quota_reset_date,
        created_at=user.created_at,
        updated_at=user.updated_at,
        chat_count=chat_count,
        message_count=message_count,
    )


@router.post("/users/{user_id}/approve")
def approve_user(
    user_id: int,
    req: UserApprovalRequest,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Approve a pending registration with user or admin access."""
    user = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.role == "pending",
        models.User.is_approved == False,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Pending user not found")
    
    user.is_approved = True
    user.role = req.role
    if req.role == "admin":
        user.token_quota = -1
    db.commit()
    
    logger.info("Admin %s approved user %s as %s", admin.username, user.username, req.role)
    return {
        "status": "success",
        "message": f"User {user.username} approved as {req.role}",
        "role": req.role,
        "token_quota": user.token_quota,
    }


@router.post("/users/{user_id}/reject")
def reject_user(
    user_id: int,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Reject (delete) a pending user registration."""
    user = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.is_approved == False,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Pending user not found")
    
    username = user.username
    db.delete(user)
    db.commit()
    
    logger.info("Admin %s rejected user %s", admin.username, username)
    return {"status": "success", "message": f"User {username} rejected and deleted"}


@router.post("/users/{user_id}/increase-quota")
def increase_user_quota(
    user_id: int,
    req: QuotaIncreaseRequest,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Increase a user's token quota (for request grants)."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user.token_quota == -1:
        return {"status": "unchanged", "message": "User has unlimited quota"}
    
    old_quota = user.token_quota
    user.token_quota += req.additional_tokens
    db.commit()
    
    logger.info("Admin %s increased quota for %s: %d -> %d (+%d)",
                admin.username, user.username, old_quota, user.token_quota, req.additional_tokens)
    
    return {
        "status": "success",
        "old_quota": old_quota,
        "new_quota": user.token_quota,
        "added": req.additional_tokens,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Statistics endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/stats/overview", response_model=OverallStatsResponse)
def get_overall_stats(
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Get overall system statistics."""
    total_users = db.query(sql_func.count(models.User.id)).scalar()
    pending_users = db.query(sql_func.count(models.User.id)).filter(
        models.User.role == "pending"
    ).scalar()
    approved_users = db.query(sql_func.count(models.User.id)).filter(
        models.User.is_approved == True
    ).scalar()
    admin_users = db.query(sql_func.count(models.User.id)).filter(
        models.User.role == "admin"
    ).scalar()
    total_chats = db.query(sql_func.count(models.ChatHistory.id)).scalar()
    total_messages = db.query(sql_func.count(models.ChatMessage.id)).scalar()
    
    # Current month stats
    now = datetime.now(timezone.utc)
    current_year, current_month = now.year, now.month
    
    total_tokens_this_month = db.query(
        sql_func.coalesce(sql_func.sum(models.TokenUsage.total_tokens), 0)
    ).filter(
        models.TokenUsage.year == current_year,
        models.TokenUsage.month == current_month,
    ).scalar()
    
    active_users_this_month = db.query(
        sql_func.count(sql_func.distinct(models.TokenUsage.user_id))
    ).filter(
        models.TokenUsage.year == current_year,
        models.TokenUsage.month == current_month,
    ).scalar()
    
    return OverallStatsResponse(
        total_users=total_users,
        pending_users=pending_users,
        approved_users=approved_users,
        admin_users=admin_users,
        total_chats=total_chats,
        total_messages=total_messages,
        total_tokens_this_month=total_tokens_this_month or 0,
        active_users_this_month=active_users_this_month or 0,
    )


@router.get("/stats/users", response_model=PaginatedResponse[UserStatsResponse])
def get_user_stats(
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    sort_by: str = Query("total_tokens", description="Sort by: total_tokens|chat_count|message_count"),
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Get per-user statistics with pagination.

    Uses three aggregate subqueries (chat/message counts, lifetime token totals,
    current-month token totals) joined to the users table — O(1) queries
    regardless of user count instead of O(N×4).
    """
    from sqlalchemy import case

    now = datetime.now(timezone.utc)
    current_year, current_month = now.year, now.month

    # ── Subquery 1: chat + message counts per user ────────────────────────
    chat_agg = (
        db.query(
            models.ChatHistory.user_id.label("user_id"),
            sql_func.count(sql_func.distinct(models.ChatHistory.id)).label("chat_count"),
            sql_func.count(models.ChatMessage.id).label("message_count"),
        )
        .outerjoin(models.ChatMessage, models.ChatMessage.history_id == models.ChatHistory.id)
        .group_by(models.ChatHistory.user_id)
        .subquery()
    )

    # ── Subquery 2: lifetime + current-month token totals per user ────────
    token_agg = (
        db.query(
            models.TokenUsage.user_id.label("user_id"),
            sql_func.coalesce(sql_func.sum(models.TokenUsage.input_tokens), 0).label("total_input"),
            sql_func.coalesce(sql_func.sum(models.TokenUsage.output_tokens), 0).label("total_output"),
            sql_func.coalesce(sql_func.sum(models.TokenUsage.reasoning_tokens), 0).label("total_reasoning"),
            sql_func.coalesce(sql_func.sum(models.TokenUsage.total_tokens), 0).label("total_tokens_all"),
            sql_func.coalesce(
                sql_func.sum(
                    case(
                        (
                            (models.TokenUsage.year == current_year) &
                            (models.TokenUsage.month == current_month),
                            models.TokenUsage.total_tokens,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("current_month_tokens"),
        )
        .group_by(models.TokenUsage.user_id)
        .subquery()
    )

    # ── Main query: users LEFT JOIN both subqueries ───────────────────────
    sort_col = {
        "chat_count": sql_func.coalesce(chat_agg.c.chat_count, 0),
        "message_count": sql_func.coalesce(chat_agg.c.message_count, 0),
    }.get(sort_by, sql_func.coalesce(token_agg.c.total_tokens_all, 0))

    base = (
        db.query(
            models.User,
            sql_func.coalesce(chat_agg.c.chat_count, 0).label("chat_count"),
            sql_func.coalesce(chat_agg.c.message_count, 0).label("message_count"),
            sql_func.coalesce(token_agg.c.total_input, 0).label("total_input"),
            sql_func.coalesce(token_agg.c.total_output, 0).label("total_output"),
            sql_func.coalesce(token_agg.c.total_reasoning, 0).label("total_reasoning"),
            sql_func.coalesce(token_agg.c.total_tokens_all, 0).label("total_tokens_all"),
            sql_func.coalesce(token_agg.c.current_month_tokens, 0).label("current_month_tokens"),
        )
        .outerjoin(chat_agg, chat_agg.c.user_id == models.User.id)
        .outerjoin(token_agg, token_agg.c.user_id == models.User.id)
        .order_by(desc(sort_col))
    )

    total = db.query(sql_func.count(models.User.id)).scalar()
    rows  = base.offset(offset).limit(limit).all()

    stats = [
        UserStatsResponse(
            user_id=row.User.id,
            username=row.User.username,
            role=row.User.role,
            chat_count=row.chat_count,
            message_count=row.message_count,
            total_input_tokens=row.total_input,
            total_output_tokens=row.total_output,
            total_reasoning_tokens=row.total_reasoning,
            total_tokens=row.total_tokens_all,
            current_month_tokens=row.current_month_tokens,
            token_quota=row.User.token_quota,
            quota_remaining=(
                -1 if row.User.token_quota == -1
                else max(0, row.User.token_quota - row.User.tokens_used_this_month)
            ),
        )
        for row in rows
    ]

    return PaginatedResponse(
        items=stats,
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + len(stats)) < total,
    )


@router.get("/stats/users/{user_id}/monthly", response_model=List[MonthlyUsageResponse])
def get_user_monthly_stats(
    user_id: int,
    months: int = Query(6, ge=1, le=24, description="Number of months to return"),
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin),
):
    """Get monthly token usage for a specific user."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    usage = db.query(models.TokenUsage).filter(
        models.TokenUsage.user_id == user_id
    ).order_by(
        desc(models.TokenUsage.year),
        desc(models.TokenUsage.month),
    ).limit(months).all()
    
    return [MonthlyUsageResponse(
        year=u.year,
        month=u.month,
        user_id=u.user_id,
        username=user.username,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        reasoning_tokens=u.reasoning_tokens,
        total_tokens=u.total_tokens,
        request_count=u.request_count,
    ) for u in usage]


# ═══════════════════════════════════════════════════════════════════════════════
# Model configuration endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/models", response_model=ModelConfigResponse)
def get_models_config(
    admin: models.User = Depends(require_admin),
):
    """Get the current models configuration."""
    config = models_config.get_config()
    return ModelConfigResponse(
        default_model=config.get("default_model", "gpt-4.1"),
        tiers=config.get("tiers", {"free": [], "paid": []}),
        models=config.get("models", {}),
    )


@router.put("/models", response_model=ModelConfigResponse)
def update_models_config(
    req: ModelConfigUpdateRequest,
    request: Request,
    admin: models.User = Depends(require_admin),
):
    """Update the models configuration (full or partial)."""
    config = models_config.get_config().copy()
    
    if req.default_model is not None:
        config["default_model"] = req.default_model
    if req.tiers is not None:
        config["tiers"] = req.tiers
    if req.models is not None:
        config["models"] = req.models
    
    models_config.save_config(config)
    _clear_agent_cache(request)
    logger.info("Admin %s updated models config", admin.username)
    
    return ModelConfigResponse(
        default_model=config.get("default_model", "gpt-4.1"),
        tiers=config.get("tiers", {"free": [], "paid": []}),
        models=config.get("models", {}),
    )


@router.post("/models/{model_id}")
def add_or_update_model(
    model_id: str,
    req: ModelEntryRequest,
    request: Request,
    admin: models.User = Depends(require_admin),
):
    """Add or update a single model entry."""
    config = models_config.get_config().copy()
    
    if "models" not in config:
        config["models"] = {}
    if "tiers" not in config:
        config["tiers"] = {"free": [], "paid": []}
    
    config["models"][model_id] = {
        "name": req.name,
        "provider": req.provider,
        "model": req.model or model_id,
        "deployment": req.deployment,
        "azure_deployment": req.azure_deployment or req.deployment,
        "endpoint": req.endpoint,
        "azure_endpoint": req.azure_endpoint or req.endpoint,
        "base_url": req.base_url,
        "api_version": req.api_version,
        "api_key_env": req.api_key_env,
        "api_key": req.api_key,
        "organization": req.organization,
        "temperature": req.temperature,
        "description": req.description,
        "context_window": req.context_window,
        "max_output": req.max_output,
        "supports_reasoning": req.supports_reasoning,
        "supports_vision": req.supports_vision,
        "enabled": req.enabled,
    }
    
    # Update tiers
    free_models = set(config["tiers"].get("free", []))
    paid_models = set(config["tiers"].get("paid", []))
    
    if req.is_free:
        free_models.add(model_id)
        paid_models.discard(model_id)
    else:
        paid_models.add(model_id)
        free_models.discard(model_id)
    
    config["tiers"]["free"] = list(free_models)
    config["tiers"]["paid"] = list(paid_models)
    
    models_config.save_config(config)
    _clear_agent_cache(request)
    logger.info("Admin %s added/updated model %s", admin.username, model_id)
    
    return {"status": "success", "model_id": model_id}


@router.delete("/models/{model_id}")
def delete_model(
    model_id: str,
    request: Request,
    admin: models.User = Depends(require_admin),
):
    """Delete a model from the configuration."""
    config = models_config.get_config().copy()
    
    if "models" not in config or model_id not in config["models"]:
        raise HTTPException(status_code=404, detail="Model not found")
    
    del config["models"][model_id]
    
    # Remove from tiers
    if "tiers" in config:
        for tier in config["tiers"].values():
            if model_id in tier:
                tier.remove(model_id)
    
    models_config.save_config(config)
    _clear_agent_cache(request)
    logger.info("Admin %s deleted model %s", admin.username, model_id)
    
    return {"status": "success", "deleted": model_id}


@router.post("/models/reload")
def reload_models_config(
    request: Request,
    admin: models.User = Depends(require_admin),
):
    """Force reload models configuration from disk."""
    config = models_config.reload_config()
    _clear_agent_cache(request)
    logger.info("Admin %s reloaded models config", admin.username)
    return {
        "status": "success",
        "model_count": len(config.get("models", {})),
    }