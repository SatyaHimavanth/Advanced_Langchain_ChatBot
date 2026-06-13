import logging
import os

from langchain.chat_models import BaseChatModel, init_chat_model
from langchain.embeddings import Embeddings, init_embeddings

from app.logger import get_logger
from app import models_config
from app.settings import settings

logger = get_logger(__name__)


def _secret_from_entry(entry: dict) -> str | None:
    if entry.get("api_key"):
        return entry["api_key"]
    if entry.get("api_key_env"):
        return os.getenv(entry["api_key_env"])
    return None


def get_llm(model_id: str | None = None) -> BaseChatModel:
    resolved_id, entry = models_config.get_model_entry(model_id)
    provider = (entry.get("provider") or "azure_openai").lower()
    model = entry.get("model") or entry.get("model_name") or settings.CHAT_MODEL or resolved_id
    deployment = (
        entry.get("azure_deployment")
        or entry.get("deployment")
        or settings.CHAT_DEPLOYMENT_NAME
    )

    kwargs = {
        "reasoning": entry.get("reasoning"),
        "stream_usage": entry.get("stream_usage", True),
    }
    if entry.get("temperature") is not None:
        kwargs["temperature"] = entry["temperature"]
    if entry.get("max_output") is not None:
        kwargs["max_completion_tokens"] = entry["max_output"]
    if api_key := _secret_from_entry(entry):
        kwargs["api_key"] = api_key

    if provider in {"azure", "azure_openai", "azure-openai", "cisco"}:
        if not deployment:
            raise RuntimeError(f"Model '{resolved_id}' requires an Azure deployment name")
        kwargs["azure_deployment"] = deployment
        if endpoint := (entry.get("azure_endpoint") or entry.get("endpoint")):
            kwargs["azure_endpoint"] = endpoint
        if api_version := entry.get("api_version"):
            kwargs["api_version"] = api_version
        return init_chat_model(
            model=model,
            model_provider="azure_openai",
            **{k: v for k, v in kwargs.items() if v is not None},
        )

    if provider == "openai":
        if base_url := (entry.get("base_url") or entry.get("endpoint")):
            kwargs["base_url"] = base_url
        if organization := entry.get("organization"):
            kwargs["organization"] = organization
        return init_chat_model(
            model=model,
            model_provider="openai",
            **{k: v for k, v in kwargs.items() if v is not None},
        )

    return init_chat_model(
        model=model,
        model_provider=provider,
        **{k: v for k, v in kwargs.items() if v is not None},
    )


def get_embedding() -> Embeddings:
    model = settings.EMBEDDING_MODEL
    deployment = settings.EMBEDDING_DEPLOYMENT_NAME

    if not model or not deployment:
        raise RuntimeError("EMBEDDING_MODEL and EMBEDDING_DEPLOYMENT_NAME must be set")

    return init_embeddings(
        model=model,
        azure_deployment=deployment,
    )
