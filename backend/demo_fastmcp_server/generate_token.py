"""
Use this script to generate bearer tokens for TaskHub MCP when running in
production JWT mode (ENVIRONMENT=production or ENVIRONMENT=staging).

In development mode the static tokens in .env are sufficient:
  dev-admin-2024    (all scopes)
  dev-user-2024     (tasks:read/write, users:read)
  dev-readonly-2024 (tasks:read)

Usage examples:
  # Admin token with all scopes (8 h default)
  python generate_token.py --role admin \\
      --scopes tasks:read tasks:write tasks:admin users:read admin

  # Read-only token, 1 hour
  python generate_token.py --subject bob --email bob@example.com \\
      --scopes tasks:read --expires 1

  # Default: test-user, tasks:read, 8 h
  python generate_token.py
"""
from __future__ import annotations

import argparse
import sys

from auth import generate_dev_token
from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="generate_token",
        description="Generate signed JWTs for TaskHub MCP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--subject", "-s",
        default="test-user",
        metavar="ID",
        help="User identifier stored in the 'sub' claim (default: test-user)",
    )
    parser.add_argument(
        "--email", "-e",
        default="test@dev.local",
        metavar="EMAIL",
        help="User email (default: test@dev.local)",
    )
    parser.add_argument(
        "--role", "-r",
        default="user",
        choices=["user", "admin", "viewer"],
        help="Role label embedded in the token (default: user)",
    )
    parser.add_argument(
        "--scopes", "-c",
        nargs="+",
        default=["tasks:read"],
        metavar="SCOPE",
        help="Space-separated list of permission scopes (default: tasks:read)",
    )
    parser.add_argument(
        "--expires", "-x",
        type=int,
        default=8,
        metavar="HOURS",
        help="Validity period in hours (default: 8)",
    )
    args = parser.parse_args()

    token = generate_dev_token(
        subject=args.subject,
        scopes=args.scopes,
        role=args.role,
        email=args.email,
        expires_in_hours=args.expires,
    )

    sep = "─" * 60
    print()
    print(sep)
    print("  TaskHub MCP — Generated JWT")
    print(sep)
    print(f"  Subject  : {args.subject}")
    print(f"  Email    : {args.email}")
    print(f"  Role     : {args.role}")
    print(f"  Scopes   : {', '.join(args.scopes)}")
    print(f"  Expires  : {args.expires}h")
    print(f"  Issuer   : {settings.jwt_issuer}")
    print(f"  Audience : {settings.jwt_audience}")
    print(sep)
    print()
    print("Bearer token:")
    print(token)
    print()
    print("Example curl:")
    print(f'  curl -H "Authorization: Bearer {token[:40]}..." \\')
    print(f"       http://{settings.host}:{settings.port}/health")
    print()


if __name__ == "__main__":
    main()
