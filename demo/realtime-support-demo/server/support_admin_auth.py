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
    secret = admin_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Admin dashboard is not configured — set SUPPORT_ADMIN_SECRET.",
        )
    token = admin_token_from_request(request)
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def verify_admin_token(token: str) -> bool:
    secret = admin_secret()
    if not secret or not token:
        return False
    return hmac.compare_digest(token.strip(), secret)
