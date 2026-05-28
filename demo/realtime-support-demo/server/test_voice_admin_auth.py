"""Tests for voice admin authentication."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from voice_admin_auth import (
    admin_auth_configured,
    require_admin_auth,
    verify_admin_token,
)


def _request(headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


class VoiceAdminAuthTests(unittest.TestCase):
    def test_not_configured_without_secret(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("REALTIME_SALES_ADMIN_SECRET", None)
            self.assertFalse(admin_auth_configured())

    def test_require_auth_rejects_bad_token(self) -> None:
        secret = "a" * 20
        with patch.dict(os.environ, {"REALTIME_SALES_ADMIN_SECRET": secret}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                require_admin_auth(_request({"Authorization": "Bearer wrong"}))
            self.assertEqual(ctx.exception.status_code, 401)

    def test_verify_accepts_admin_test_password(self) -> None:
        with patch.dict(os.environ, {"REALTIME_SALES_ADMIN_SECRET": "Admin"}, clear=False):
            self.assertTrue(admin_auth_configured())
            self.assertTrue(verify_admin_token("Admin"))


if __name__ == "__main__":
    unittest.main()
