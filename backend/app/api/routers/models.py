"""
models.py (router)
──────────────────
Public API for available models and user quota information.

These endpoints are available to all authenticated users (not admin-only).
"""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import database, models
from app.core import auth
from app import models_config as mc

router = APIRouter(prefix="/models", tags=["models"])


# ═══════════════════════════════════════════════════════════════════════════════
# Response schemas
# ═══════════════════════════════════════════════════════════════════════════════

class ModelInfo(BaseModel):
    """Model information for the UI dropdown."""
    id: str
    name: str
    provider: str
    description: str
    is_free: bool
    supports_reasoning: bool
    supports_vision: bool
    context_window: int
    max_output: int


class ModelsListResponse(BaseModel):
    """Response with available models and user's quota info."""
    models: List[ModelInfo]
    default_model: str
    # User quota info
    quota_used: int
    quota_total: int  # -1 means unlimited
    quota_remaining: int  # -1 means unlimited


class UserQuotaResponse(BaseModel):
    """User's current token quota and usage."""
    tokens_used_this_month: int
    token_quota: int  # -1 = unlimited
    quota_remaining: int  # -1 = unlimited
    quota_reset_date: datetime | None
    is_unlimited: bool


# ═══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/", response_model=ModelsListResponse)
def list_available_models(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    List all available models for the user to select from.
    
    Returns enabled models from the config, sorted with free models first.
    Also returns the user's current quota information.
    """
    model_list = mc.list_models(include_disabled=False)
    
    models_response = [
        ModelInfo(
            id=m.id,
            name=m.name,
            provider=m.provider,
            description=m.description,
            is_free=m.is_free,
            supports_reasoning=m.supports_reasoning,
            supports_vision=m.supports_vision,
            context_window=m.context_window,
            max_output=m.max_output,
        )
        for m in model_list
    ]
    
    quota_remaining = -1 if current_user.token_quota == -1 else max(
        0, current_user.token_quota - current_user.tokens_used_this_month
    )
    
    return ModelsListResponse(
        models=models_response,
        default_model=mc.get_default_model(),
        quota_used=current_user.tokens_used_this_month,
        quota_total=current_user.token_quota,
        quota_remaining=quota_remaining,
    )


@router.get("/quota", response_model=UserQuotaResponse)
def get_my_quota(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Get the current user's token quota and usage."""
    is_unlimited = current_user.token_quota == -1
    quota_remaining = -1 if is_unlimited else max(
        0, current_user.token_quota - current_user.tokens_used_this_month
    )
    
    return UserQuotaResponse(
        tokens_used_this_month=current_user.tokens_used_this_month,
        token_quota=current_user.token_quota,
        quota_remaining=quota_remaining,
        quota_reset_date=current_user.quota_reset_date,
        is_unlimited=is_unlimited,
    )


@router.get("/{model_id}", response_model=ModelInfo)
def get_model_info(
    model_id: str,
    current_user: models.User = Depends(auth.get_current_user),
):
    """Get information about a specific model."""
    info = mc.get_model_info(model_id)
    if not info or not info.enabled:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Model not found or disabled")
    
    return ModelInfo(
        id=info.id,
        name=info.name,
        provider=info.provider,
        description=info.description,
        is_free=info.is_free,
        supports_reasoning=info.supports_reasoning,
        supports_vision=info.supports_vision,
        context_window=info.context_window,
        max_output=info.max_output,
    )
