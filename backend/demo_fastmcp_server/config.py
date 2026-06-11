from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    """
    All configuration is read from environment variables (or .env via python-dotenv).
    Sensible defaults are provided for local development only — override every
    value in staging/production.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    name: str = field(
        default_factory=lambda: os.environ.get("SERVER_NAME", "TaskHub MCP")
    )
    version: str = "1.0.0"
    environment: str = field(
        default_factory=lambda: os.environ.get("ENVIRONMENT", "development")
    )

    # ── Transport ─────────────────────────────────────────────────────────────
    host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8000")))

    # ── JWT Auth ──────────────────────────────────────────────────────────────
    # For HS256 (default): jwt_secret is the HMAC shared secret.
    # For RS256 / external IdP: swap JWTVerifier for one with jwks_uri=.
    jwt_secret: str = field(
        default_factory=lambda: os.environ.get(
            "JWT_SECRET",
            "CHANGE-ME-in-production-must-be-32+-chars!!",
        )
    )
    jwt_issuer: str = field(
        default_factory=lambda: os.environ.get("JWT_ISSUER", "taskhub-auth-service")
    )
    jwt_audience: str = field(
        default_factory=lambda: os.environ.get("JWT_AUDIENCE", "taskhub-mcp-server")
    )
    jwt_algorithm: str = "HS256"

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_max_requests: int = field(
        default_factory=lambda: int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "120"))
    )
    rate_limit_window_minutes: int = field(
        default_factory=lambda: int(os.environ.get("RATE_LIMIT_WINDOW_MINUTES", "1"))
    )

    # ── Behaviour ─────────────────────────────────────────────────────────────
    # mask_error_details=True prevents internal stack traces leaking to LLM clients.
    mask_error_details: bool = field(
        default_factory=lambda: os.environ.get("MASK_ERRORS", "true").lower() == "true"
    )
    # Hard cap on tool response size — protects LLM context windows.
    max_response_bytes: int = field(
        default_factory=lambda: int(os.environ.get("MAX_RESPONSE_BYTES", "500000"))
    )
    # Cursor-based pagination for list operations.
    list_page_size: int = field(
        default_factory=lambda: int(os.environ.get("LIST_PAGE_SIZE", "50"))
    )

    # ── Dev Tokens ────────────────────────────────────────────────────────────
    # Only active when ENVIRONMENT=development.
    # StaticTokenVerifier + AuthContextMiddleware both consume this mapping.
    dev_admin_token: str = field(
        default_factory=lambda: os.environ.get("DEV_ADMIN_TOKEN", "dev-admin-2024")
    )
    dev_user_token: str = field(
        default_factory=lambda: os.environ.get("DEV_USER_TOKEN", "dev-user-2024")
    )
    dev_readonly_token: str = field(
        default_factory=lambda: os.environ.get("DEV_READONLY_TOKEN", "dev-readonly-2024")
    )

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


# Module-level singleton — import this everywhere.
settings = Settings()
