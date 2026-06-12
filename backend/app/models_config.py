"""
models_config.py
────────────────
Configuration loader for available LLM models.

Reads models.yaml on startup and provides helpers for:
  - Listing available models for the UI
  - Checking if a model is free (doesn't consume quota)
  - Getting model metadata (provider, deployment, capabilities)
  - Saving config changes from admin UI
"""

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.logger import get_logger

logger = get_logger(__name__)

# Path to models config file
MODELS_CONFIG_PATH = Path(__file__).parent / "models.yaml"

# Thread lock for safe concurrent access
_lock = threading.RLock()

# Cached config (reloaded when file changes or on explicit reload)
_config: dict[str, Any] | None = None
_config_mtime: float = 0


class ModelInfo(BaseModel):
    """Model metadata returned to frontend."""
    id: str
    name: str
    provider: str
    deployment: str | None = None
    description: str = ""
    context_window: int = 128000
    max_output: int = 16384
    supports_reasoning: bool = False
    supports_vision: bool = False
    enabled: bool = True
    is_free: bool = False


def _load_config() -> dict[str, Any]:
    """Load config from YAML file."""
    global _config, _config_mtime
    
    if not MODELS_CONFIG_PATH.exists():
        logger.warning("models.yaml not found at %s, using defaults", MODELS_CONFIG_PATH)
        return {
            "default_model": "gpt-4.1",
            "tiers": {"free": [], "paid": []},
            "models": {},
        }
    
    try:
        mtime = MODELS_CONFIG_PATH.stat().st_mtime
        # Return cached if file unchanged
        if _config is not None and mtime == _config_mtime:
            return _config
        
        with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        
        _config = config
        _config_mtime = mtime
        logger.info("Loaded models config: %d models", len(config.get("models", {})))
        return config
    except Exception as exc:
        logger.exception("Failed to load models.yaml: %s", exc)
        return _config or {"default_model": "gpt-4.1", "tiers": {"free": [], "paid": []}, "models": {}}


def get_config() -> dict[str, Any]:
    """Get current models config (auto-reloads if file changed)."""
    with _lock:
        return _load_config()


def reload_config() -> dict[str, Any]:
    """Force reload config from disk."""
    global _config, _config_mtime
    with _lock:
        _config = None
        _config_mtime = 0
        return _load_config()


def save_config(config: dict[str, Any]) -> None:
    """Save config to YAML file (from admin UI)."""
    global _config, _config_mtime
    with _lock:
        with open(MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        _config = config
        _config_mtime = MODELS_CONFIG_PATH.stat().st_mtime
        logger.info("Saved models config: %d models", len(config.get("models", {})))


def get_default_model() -> str:
    """Get the default model ID."""
    return get_config().get("default_model", "gpt-4.1")


def get_free_models() -> set[str]:
    """Get set of model IDs that don't consume quota."""
    config = get_config()
    return set(config.get("tiers", {}).get("free", []))


def is_free_model(model_id: str) -> bool:
    """Check if a model doesn't consume token quota."""
    return model_id in get_free_models()


def get_model_info(model_id: str) -> ModelInfo | None:
    """Get metadata for a specific model."""
    config = get_config()
    models = config.get("models", {})
    
    if model_id not in models:
        return None
    
    m = models[model_id]
    return ModelInfo(
        id=model_id,
        name=m.get("name", model_id),
        provider=m.get("provider", "unknown"),
        deployment=m.get("deployment"),
        description=m.get("description", ""),
        context_window=m.get("context_window", 128000),
        max_output=m.get("max_output", 16384),
        supports_reasoning=m.get("supports_reasoning", False),
        supports_vision=m.get("supports_vision", False),
        enabled=m.get("enabled", True),
        is_free=is_free_model(model_id),
    )


def list_models(include_disabled: bool = False) -> list[ModelInfo]:
    """List all available models for the UI."""
    config = get_config()
    models = config.get("models", {})
    free_models = get_free_models()
    
    result = []
    for model_id, m in models.items():
        if not include_disabled and not m.get("enabled", True):
            continue
        result.append(ModelInfo(
            id=model_id,
            name=m.get("name", model_id),
            provider=m.get("provider", "unknown"),
            deployment=m.get("deployment"),
            description=m.get("description", ""),
            context_window=m.get("context_window", 128000),
            max_output=m.get("max_output", 16384),
            supports_reasoning=m.get("supports_reasoning", False),
            supports_vision=m.get("supports_vision", False),
            enabled=m.get("enabled", True),
            is_free=model_id in free_models,
        ))
    
    # Sort: free models first, then by name
    result.sort(key=lambda x: (not x.is_free, x.name))
    return result


def get_deployment_name(model_id: str) -> str:
    """Get the deployment name for a model (for Azure OpenAI)."""
    info = get_model_info(model_id)
    if info and info.deployment:
        return info.deployment
    return model_id  # Fallback to model_id as deployment
