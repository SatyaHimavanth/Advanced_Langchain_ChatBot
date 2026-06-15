import json
import os
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from dotenv import load_dotenv
load_dotenv()


def _env(name: str, default: str = "") -> str:
    """Trimmed environment lookup with a default."""
    val = os.getenv(name)
    return val if val is not None else default


def _env_json(name: str, default: Any = None) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


class Settings(BaseModel):
    # ── Application relational DB (users / chats) — SQLAlchemy ───────────────
    SQLALCHEMY_DATABASE_URL: str = _env(
        "SQLALCHEMY_DATABASE_URL", "sqlite:///./agent.db"
    )

    # ── Agent persistence (LangGraph store + checkpointer) ──────────────────
    STORE_DATABASE_URL: str = _env("STORE_DATABASE_URL") or _env(
        "SQLALCHEMY_DATABASE_URL"
    )

    # ── Per-user agent workspace (real files on disk) ───────────────────────
    WORKSPACE_ROOT: str = _env(
        "WORKSPACE_ROOT", str(Path.cwd() / "workspace")
    )

    # ── CORS origins for the frontend ───────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    # ── Host command execution sandboxing / watcher settings ──────────────────
    ENABLE_HOST_EXECUTION: bool = _env("ENABLE_HOST_EXECUTION", "True").lower() == "true"
    SKILLS_WATCH: bool = _env("SKILLS_WATCH", "False").lower() == "true"

    # ── App auth (FastAPI JWT for the chat backend itself) ──────────────────
    # No hardcoded fallback — the server refuses to start without this set.
    # Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET_KEY: str = _env("JWT_SECRET_KEY", "")
    JWT_ALGORITHM: str = _env("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(_env("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(_env("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

    # ── Admin configuration ─────────────────────────────────────────────────
    ADMIN_USERNAME: str = _env("ADMIN_USERNAME", "Admin")
    ADMIN_PASSWORD: str = _env("ADMIN_PASSWORD", "")

    # Default token quota for new users (per month). -1 = unlimited
    DEFAULT_TOKEN_QUOTA: int = int(_env("DEFAULT_TOKEN_QUOTA", "100000"))

    # Days before pending user registrations are auto-rejected (0 = never auto-reject)
    PENDING_USER_EXPIRE_DAYS: int = int(_env("PENDING_USER_EXPIRE_DAYS", "7"))

    # ── Generic chat-model (init_chat_model / init_embeddings) ─────
    CHAT_MODEL: str = _env("CHAT_MODEL")
    CHAT_DEPLOYMENT_NAME: str = _env("CHAT_DEPLOYMENT_NAME")
    EMBEDDING_MODEL: str = _env("EMBEDDING_MODEL")
    EMBEDDING_DEPLOYMENT_NAME: str = _env("EMBEDDING_DEPLOYMENT_NAME")

    # ── MCP servers (langchain-mcp-adapters MultiServerMCPClient) ───────────
    MCP_SERVERS: dict[str, Any] = Field(
        default_factory=lambda: _env_json("MCP_SERVERS", {}) or {}
    )
    MCP_SERVER_NAME: str = _env("MCP_SERVER_NAME", "taskhub")
    MCP_SERVER_URL: str = _env("MCP_SERVER_URL", "")
    MCP_SERVER_AUTH_TOKEN: str = _env("MCP_SERVER_AUTH_TOKEN", "")
    MCP_INJECT_PROMPT_CONTENT: bool = _env(
        "MCP_INJECT_PROMPT_CONTENT", "false"
    ).lower() == "true"
    MCP_INCLUDE_TOOLS: bool = _env("MCP_INCLUDE_TOOLS", "false").lower() == "true"

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        # JWT secret — hard failure. A missing or public key means every token
        # issued by this server can be forged by anyone with the source code.
        if not self.JWT_SECRET_KEY:
            raise ValueError(
                "JWT_SECRET_KEY is not set in your environment / .env file.\n"
                "Generate a secure key with:\n"
                "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
                "then add JWT_SECRET_KEY=<value> to your .env."
            )

        # Admin password — hard failure. An empty or well-known default password
        # on a publicly known admin account is an immediate compromise vector.
        _weak = {"", "admin", "password", "changeme", "secret"}
        if self.ADMIN_PASSWORD.lower() in _weak:
            raise ValueError(
                "ADMIN_PASSWORD is not set or is using an insecure default.\n"
                "Set a strong ADMIN_PASSWORD in your .env file."
            )

        return self

    def mcp_server_config(self) -> dict[str, Any]:
        """
        Resolve the effective MCP server config dict:
          • MCP_SERVERS (JSON map) wins if provided.
          • Otherwise build a single-server dict from MCP_SERVER_URL/_TOKEN/_NAME.
        Returns {} when MCP is not configured.
        """
        if self.MCP_SERVERS:
            return self.MCP_SERVERS
        if not self.MCP_SERVER_URL:
            return {}
        entry: dict[str, Any] = {
            "transport": "streamable_http",
            "url": self.MCP_SERVER_URL,
        }
        if self.MCP_SERVER_AUTH_TOKEN:
            entry["headers"] = {
                "Authorization": f"Bearer {self.MCP_SERVER_AUTH_TOKEN}"
            }
        return {self.MCP_SERVER_NAME: entry}


settings = Settings()