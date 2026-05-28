"""Support Control dashboard API."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from hubspot_kb_sync import hubspot_kb_sync_status, run_hubspot_kb_sync_async
from slack_sync import run_slack_sync, slack_sync_status
from support_admin_auth import admin_auth_configured
from support_dashboard_store import (
    SETTING_KEYS,
    clear_settings,
    get_all_settings,
    get_session,
    list_active_sessions,
    list_sessions,
    set_settings,
    support_stats,
)
from support_instructions import get_default_prompts


class SupportSettingsPatch(BaseModel):
    support_voice_prompt: str | None = None
    support_chat_prompt: str | None = None
    chat_model: str | None = None


def dashboard_overview() -> dict[str, Any]:
    stats = support_stats()
    slack = slack_sync_status()
    hubspot = hubspot_kb_sync_status()
    return {
        "configured": admin_auth_configured(),
        "stats": stats,
        "active_sessions": list_active_sessions(),
        "slack_sync": slack,
        "hubspot_kb_sync": hubspot,
    }


def dashboard_calls(*, limit: int = 100) -> dict[str, Any]:
    return {"calls": list_sessions(limit=limit)}


def dashboard_call_detail(call_id: str) -> dict[str, Any]:
    row = get_session(call_id)
    if not row:
        return {"ok": False, "error": "Session not found"}
    return {"ok": True, "call": row}


def dashboard_settings_get() -> dict[str, Any]:
    defaults = get_default_prompts()
    overrides = get_all_settings()
    env_model = os.environ.get("SUPPORT_CHAT_MODEL", "gpt-4o-mini").strip()
    return {
        "defaults": defaults,
        "overrides": {k: overrides[k] for k in SETTING_KEYS if k in overrides},
        "effective": {
            "support_voice_prompt": overrides.get("support_voice_prompt") or defaults["support_voice_prompt"],
            "support_chat_prompt": overrides.get("support_chat_prompt") or defaults["support_chat_prompt"],
            "chat_model": overrides.get("chat_model") or env_model,
        },
    }


def dashboard_settings_patch(body: SupportSettingsPatch) -> dict[str, Any]:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if patch:
        set_settings(patch)
    return {"ok": True, "settings": dashboard_settings_get()}


def dashboard_settings_reset() -> dict[str, Any]:
    clear_settings()
    return {"ok": True, "settings": dashboard_settings_get()}


async def dashboard_slack_sync(*, full_backfill: bool = False) -> dict[str, Any]:
    return run_slack_sync(full_backfill=full_backfill)


async def dashboard_hubspot_kb_sync() -> dict[str, Any]:
    return await run_hubspot_kb_sync_async()
