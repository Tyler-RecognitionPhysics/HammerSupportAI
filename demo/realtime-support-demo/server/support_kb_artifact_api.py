"""Serve KB artifacts from the persistent sync host and proxy sync from Vercel."""

from __future__ import annotations

import hmac
import logging
import os

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from support_admin_auth import admin_secret, admin_token_from_request

_log = logging.getLogger(__name__)


def _artifact_auth(request: Request) -> None:
    from knowledge_support.kb_artifact import artifact_token

    provided = admin_token_from_request(request)
    if not provided:
        raise HTTPException(401, "Unauthorized")
    allowed = {s for s in (artifact_token(), admin_secret()) if s}
    if not allowed:
        raise HTTPException(503, "Artifact token not configured")
    if not any(hmac.compare_digest(provided, secret) for secret in allowed):
        raise HTTPException(401, "Unauthorized")


def knowledge_artifact_manifest(request: Request) -> JSONResponse:
    _artifact_auth(request)
    from knowledge_support.kb_artifact import manifest_path, read_manifest, write_manifest

    manifest = read_manifest()
    if manifest is None:
        try:
            manifest = write_manifest()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
    return JSONResponse(manifest)


def knowledge_artifact_sqlite(request: Request) -> FileResponse:
    _artifact_auth(request)
    from knowledge_support.kb_artifact import kb_db_path

    path = kb_db_path()
    if not path.is_file():
        raise HTTPException(404, "support_kb.sqlite not found")
    return FileResponse(path, media_type="application/octet-stream", filename="support_kb.sqlite")


def knowledge_artifact_tickets_state(request: Request) -> FileResponse:
    _artifact_auth(request)
    from knowledge_support.kb_artifact import tickets_state_path

    path = tickets_state_path()
    if not path.is_file():
        raise HTTPException(404, "hubspot_tickets_sync.sqlite not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename="hubspot_tickets_sync.sqlite",
    )


async def proxy_hubspot_tickets_sync(
    request: Request,
    *,
    full_backfill: bool,
    background: bool,
) -> dict:
    sync_host = os.environ.get("SUPPORT_SYNC_HOST_URL", "").strip().rstrip("/")
    if not sync_host:
        raise HTTPException(503, "SUPPORT_SYNC_HOST_URL not configured")

    token = admin_token_from_request(request)
    if not token:
        raise HTTPException(401, "Unauthorized")

    url = f"{sync_host}/api/admin/support/knowledge/hubspot-tickets/sync"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"full_backfill": full_backfill, "background": background},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        _log.exception("sync proxy failed")
        raise HTTPException(502, f"Persistent sync host error: {exc}") from exc


async def run_hubspot_tickets_sync_handler(
    request: Request,
    *,
    full_backfill: bool,
    background: bool,
    is_production: bool,
    invalidate_cache,
    warm_retriever,
) -> dict:
    if is_production():
        sync_host = os.environ.get("SUPPORT_SYNC_HOST_URL", "").strip()
        if sync_host:
            return await proxy_hubspot_tickets_sync(
                request,
                full_backfill=full_backfill,
                background=background,
            )
        raise HTTPException(
            503,
            "Ticket sync runs on the persistent host. Set SUPPORT_SYNC_HOST_URL on Vercel.",
        )

    from hubspot_tickets_sync import (
        hubspot_tickets_sync_status,
        run_hubspot_tickets_sync_async,
        start_hubspot_tickets_sync_background,
    )

    if background:
        return await start_hubspot_tickets_sync_background(full_backfill=full_backfill)

    result = await run_hubspot_tickets_sync_async(full_backfill=full_backfill)
    if result.get("ok"):
        invalidate_cache()
        warm_retriever()
    result["status"] = hubspot_tickets_sync_status()
    return result
