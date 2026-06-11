"""
Two operating modes, selected by ENVIRONMENT env var:

  development  →  StaticTokenVerifier   (opaque token → claims dict lookup)
  production   →  JWTVerifier / HS256   (validated, signed JWT)

For an external IdP (Auth0, WorkOS, Cognito, etc.) replace the JWTVerifier
with one that uses jwks_uri= for automatic public-key rotation.
"""
from __future__ import annotations

import os
import time
from typing import Any

import jwt as pyjwt

from config import settings

# ── Dev token map ──────────────────────────────────────────────────────────────
# Shared by StaticTokenVerifier (FastMCP layer) AND AuthContextMiddleware
# (claim extraction layer) so both resolve the same token → claims in dev.
#
# Each entry mirrors JWT payload shape so AuthContextMiddleware can treat
# both dev and prod tokens identically when storing session state.
DEV_TOKENS: dict[str, dict[str, Any]] = {
    settings.dev_admin_token: {
        "sub": "dev-admin",
        "email": "admin@dev.local",
        "role": "admin",
        "scopes": [
            "tasks:read",
            "tasks:write",
            "tasks:admin",
            "users:read",
            "admin",
        ],
    },
    settings.dev_user_token: {
        "sub": "dev-user",
        "email": "user@dev.local",
        "role": "user",
        "scopes": ["tasks:read", "tasks:write", "users:read"],
    },
    settings.dev_readonly_token: {
        "sub": "dev-readonly",
        "email": "readonly@dev.local",
        "role": "viewer",
        "scopes": ["tasks:read"],
    },
}


def build_auth_provider():
    """
    Return the appropriate FastMCP auth provider for the current environment.

    development → StaticTokenVerifier  (no JWT infra needed, instant start)
    production  → JWTVerifier / HS256  (replace with JWKS for key rotation)
    """
    if settings.is_development:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        # StaticTokenVerifier expects: { token_str: { "client_id": ..., "scopes": [...] } }
        # We adapt our DEV_TOKENS map to that shape.
        return StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": claims["sub"],
                    "scopes": claims["scopes"],
                }
                for token, claims in DEV_TOKENS.items()
            },
            required_scopes=["tasks:read"],  # Minimum scope to connect at all
        )

    # Production: HMAC-signed JWTs.
    # For asymmetric keys (RS256/ES256), set:
    #   jwks_uri="https://your-idp.example.com/.well-known/jwks.json"
    # and remove public_key / algorithm.
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    return JWTVerifier(
        public_key=settings.jwt_secret,   # For HS256 the "public_key" IS the shared secret
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        algorithm=settings.jwt_algorithm,
        required_scopes=["tasks:read"],
    )


def generate_dev_token(
    subject: str,
    scopes: list[str],
    role: str = "user",
    email: str = "",
    expires_in_hours: int = 8,
) -> str:
    """
    Sign a JWT for development / integration testing.

    Args:
        subject:          User identifier stored in the 'sub' claim.
        scopes:           List of permission strings stored in 'scopes'.
        role:             Human-readable role label.
        email:            Caller's email address.
        expires_in_hours: How long the token is valid.

    Returns:
        Compact, signed JWT string.

    ⚠️  For development use only.
        In production, tokens are issued exclusively by your auth service.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "role": role,
        "scopes": scopes,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + (expires_in_hours * 3_600),
        "jti": os.urandom(16).hex(),  # Unique ID — prevents naïve replay attacks
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
