"""Remote dashboard plumbing for serverless (Vercel) deployments.

Vercel functions only have per-instance /tmp storage, so anything written
there (sessions, callbacks, ticket records, settings) is invisible to other
instances and lost when the instance is recycled — the dashboard looked
"random" because it only showed data from whichever instance answered.

When SUPPORT_SYNC_HOST_URL points at the persistent Fly host (which runs this
same app with a mounted volume), serverless deployments:
  - proxy every /api/admin/support/* request to Fly (single source of truth)
  - mirror AI-runtime store writes (sessions, callbacks, tickets) to Fly
  - read settings through from Fly with a short TTL cache

Set SUPPORT_REMOTE_DASHBOARD=0 to force fully local behavior.
"""

from __future__ import annotations

import logging
import os
import threading

import httpx
from fastapi import Request, Response

_log = logging.getLogger(__name__)

# Endpoints that must run on THIS deployment, not the persistent host.
_LOCAL_ONLY_ADMIN_PATHS = (
    "/api/admin/support/auth",        # just verifies the shared admin secret
    "/api/admin/support/knowledge/reload",  # refreshes this instance's /tmp KB copy
)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def sync_host_url() -> str:
    return os.environ.get("SUPPORT_SYNC_HOST_URL", "").strip().rstrip("/")


def remote_dashboard_enabled() -> bool:
    if os.environ.get("SUPPORT_REMOTE_DASHBOARD", "").strip() == "0":
        return False
    return _is_serverless() and bool(sync_host_url())


def internal_token() -> str:
    return (
        os.environ.get("SUPPORT_KB_ARTIFACT_TOKEN", "").strip()
        or os.environ.get("SUPPORT_ADMIN_SECRET", "").strip()
    )


def should_proxy_admin_path(path: str) -> bool:
    if not path.startswith("/api/admin/support"):
        return False
    if path in _LOCAL_ONLY_ADMIN_PATHS:
        return False
    return remote_dashboard_enabled()


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                timeout=httpx.Timeout(120.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=120.0),
            )
    return _client


async def forward_admin_request(request: Request) -> Response:
    """Proxy an admin request to the persistent host, preserving auth + body."""
    url = f"{sync_host_url()}{request.url.path}"
    headers = {}
    for name in ("authorization", "content-type"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    body = await request.body()

    def _do_request() -> httpx.Response:
        return _get_client().request(
            request.method,
            url,
            params=dict(request.query_params),
            content=body or None,
            headers=headers,
        )

    import asyncio

    resp = await asyncio.to_thread(_do_request)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def push_store_op(op: str, data: dict, *, wait: bool = True) -> bool:
    """Mirror a store write to the persistent host.

    wait=True blocks (~100ms) so the write is delivered before the serverless
    runtime freezes; wait=False is best-effort for writes that a later
    synchronous op will supersede anyway (e.g. session_start before session_save).
    """
    if not remote_dashboard_enabled():
        return False
    token = internal_token()
    if not token:
        return False

    def _send() -> bool:
        try:
            resp = _get_client().post(
                f"{sync_host_url()}/api/internal/store-sync",
                json={"op": op, "data": data},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code >= 400:
                _log.warning("store-sync push op=%s failed: %s %s", op, resp.status_code, resp.text[:200])
                return False
            return True
        except Exception:
            _log.warning("store-sync push op=%s failed", op, exc_info=True)
            return False

    if wait:
        return _send()
    threading.Thread(target=_send, daemon=True, name="store-sync").start()
    return True


def fetch_remote_settings() -> dict | None:
    """Settings live on the persistent host; voice/chat on serverless read them
    through this call (cached by the store). Returns None on any failure."""
    if not remote_dashboard_enabled():
        return None
    token = internal_token()
    if not token:
        return None
    try:
        resp = _get_client().post(
            f"{sync_host_url()}/api/internal/store-sync",
            json={"op": "settings_get", "data": {}},
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        if resp.status_code >= 400:
            return None
        payload = resp.json()
        values = payload.get("values")
        return values if isinstance(values, dict) else None
    except Exception:
        return None
