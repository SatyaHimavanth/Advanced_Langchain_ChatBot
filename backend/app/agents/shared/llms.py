import logging

from langchain.chat_models import BaseChatModel, init_chat_model
from langchain.embeddings import Embeddings, init_embeddings

from app.logger import get_logger
from app.settings import settings

logger = get_logger(__name__)


def get_llm() -> BaseChatModel:
    model = settings.CHAT_MODEL
    deployment = settings.CHAT_DEPLOYMENT_NAME

    if not model or not deployment:
        raise RuntimeError("CHAT_MODEL and CHAT_DEPLOYMENT_NAME must be set")

    return init_chat_model(
        model=model,
        azure_deployment=deployment,
        reasoning=None,
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
