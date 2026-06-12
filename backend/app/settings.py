import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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
    # Used by db/database.py. Accepts a bare postgresql:// URL (normalized to
    # the psycopg v3 driver in database.py) or sqlite:/// for local dev.
    SQLALCHEMY_DATABASE_URL: str = _env(
        "SQLALCHEMY_DATABASE_URL", "sqlite:///./agent.db"
    )

    # ── Agent persistence (LangGraph store + checkpointer) ──────────────────
    # Falls back to the application DB URL so "Postgres for everything" works
    # out of the box. Leave empty to use in-memory store/checkpointer.
    STORE_DATABASE_URL: str = _env("STORE_DATABASE_URL") or _env(
        "SQLALCHEMY_DATABASE_URL"
    )

    # ── Per-user agent workspace (real files on disk) ───────────────────────
    WORKSPACE_ROOT: str = _env(
        "WORKSPACE_ROOT", str(Path.cwd() / "workspace")
    )

    # ── Skills directory (loaded by SkillsMiddleware) ───────────────────────
    SKILLS_DIR: str = _env(
        "SKILLS_DIR", str(Path.cwd().parent / ".agents" / "skills")
    )

    # ── CORS origins for the frontend ───────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    # ── App auth (FastAPI JWT for the chat backend itself) ──────────────────
    JWT_SECRET_KEY: str = _env(
        "JWT_SECRET_KEY",
        "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7",
    )
    JWT_ALGORITHM: str = _env("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(_env("ACCESS_TOKEN_EXPIRE_MINUTES", "1"))
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(_env("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

    # ── Admin configuration ─────────────────────────────────────────────────
    # Default admin account (created on startup if doesn't exist)
    ADMIN_USERNAME: str = _env("ADMIN_USERNAME", "Admin")
    ADMIN_PASSWORD: str = _env("ADMIN_PASSWORD", "admin123")
    
    # Default token quota for new users (per month). -1 = unlimited
    DEFAULT_TOKEN_QUOTA: int = int(_env("DEFAULT_TOKEN_QUOTA", "100000"))
    
    # Whether new user registrations require admin approval
    REQUIRE_APPROVAL: bool = _env("REQUIRE_APPROVAL", "false").lower() == "true"
    
    # Days before pending user registrations are auto-rejected (0 = never auto-reject)
    PENDING_USER_EXPIRE_DAYS: int = int(_env("PENDING_USER_EXPIRE_DAYS", "7"))

    # ── Generic chat-model (init_chat_model / init_embeddings) ─────
    CHAT_MODEL: str = _env("CHAT_MODEL")
    CHAT_DEPLOYMENT_NAME: str = _env("CHAT_DEPLOYMENT_NAME")
    EMBEDDING_MODEL: str = _env("EMBEDDING_MODEL")
    EMBEDDING_DEPLOYMENT_NAME: str = _env("EMBEDDING_DEPLOYMENT_NAME")

    # ── MCP servers (langchain-mcp-adapters MultiServerMCPClient) ───────────
    # Set MCP_SERVERS as a JSON object mapping server-name → connection config:
    #   MCP_SERVERS='{"taskhub": {"transport":"streamable_http",
    #                              "url":"http://localhost:5000/mcp",
    #                              "headers":{"Authorization":"Bearer dev-admin-2024"}}}'
    # Or use the simple MCP_SERVER_URL + MCP_SERVER_AUTH_TOKEN form below for
    # a single server. Empty/unset → MCP middleware is disabled.
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