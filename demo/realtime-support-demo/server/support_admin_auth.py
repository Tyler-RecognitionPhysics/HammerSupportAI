"""Password protection for Support Control dashboard."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field


class AdminAuthRequest(BaseModel):
    secret: str = Field(..., min_length=1)


def admin_secret() -> str:
    return os.environ.get("SUPPORT_ADMIN_SECRET", "").strip()


def admin_auth_configured() -> bool:
    return bool(admin_secret())


def _extract_bearer(header: str) -> str:
    raw = header.strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def admin_token_from_request(request: Request) -> str:
    bearer = _extract_bearer(request.headers.get("authorization", ""))
    if bearer:
        return bearer
    return (request.headers.get("x-admin-secret") or "").strip()


def require_admin_auth(request: Request) -> None:
    """Password protection disabled — the dashboard is open to anyone with the URL.

    Kept as a no-op so every route's `require_admin_auth(request)` call keeps
    working; re-enable by restoring the SUPPORT_ADMIN_SECRET check here.
    """
    return None


def verify_admin_token(token: str) -> bool:
    secret = admin_secret()
    if not secret or not token:
        return False
    return hmac.compare_digest(token.strip(), secret)
