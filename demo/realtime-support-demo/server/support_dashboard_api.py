"""Support Control dashboard API."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from hubspot_kb_sync import hubspot_kb_sync_status, run_hubspot_kb_sync_async
from hubspot_tickets_sync import hubspot_tickets_sync_status, run_hubspot_tickets_sync_async
from slack_sync import run_slack_sync, slack_sync_status
from support_admin_auth import admin_auth_configured
from hubspot_ticket_create import hubspot_ticket_create_configured
from support_dashboard_store import (
    SETTING_KEYS,
    clear_settings,
    create_appointment,
    delete_appointment,
    get_all_settings,
    get_session,
    list_active_sessions,
    list_appointments,
    list_sessions,
    list_support_tickets,
    set_settings,
    support_stats,
    update_appointment,
)
from support_ticket_slack import slack_ticket_notify_configured
from support_instructions import get_default_prompts


class SupportSettingsPatch(BaseModel):
    support_voice_prompt: str | None = None
    support_chat_prompt: str | None = None
    chat_model: str | None = None


class AppointmentCreate(BaseModel):
    requested_at: str = ""
    requested_label: str = ""
    duration_min: int = 30
    dealership_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    reason: str = ""
    status: str = "requested"
    timezone: str = ""
    notes: str = ""


class AppointmentUpdate(BaseModel):
    requested_at: str | None = None
    requested_label: str | None = None
    duration_min: int | None = None
    dealership_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    reason: str | None = None
    status: str | None = None
    timezone: str | None = None
    notes: str | None = None


def dashboard_overview() -> dict[str, Any]:
    stats = support_stats()
    slack = slack_sync_status()
    hubspot = hubspot_kb_sync_status()
    hubspot_tickets = hubspot_tickets_sync_status()
    return {
        "configured": admin_auth_configured(),
        "stats": stats,
        "active_sessions": list_active_sessions(),
        "slack_sync": slack,
        "hubspot_kb_sync": hubspot,
        "hubspot_tickets_sync": hubspot_tickets,
        "hubspot_ticket_create_configured": hubspot_ticket_create_configured(),
        "slack_ticket_notify_configured": slack_ticket_notify_configured(),
    }


def dashboard_calls(*, limit: int = 100) -> dict[str, Any]:
    return {"calls": list_sessions(limit=limit)}


def dashboard_call_detail(call_id: str) -> dict[str, Any]:
    row = get_session(call_id)
    if not row:
        return {"ok": False, "error": "Session not found"}
    return {"ok": True, "call": row}


def dashboard_tickets(*, limit: int = 50) -> dict[str, Any]:
    return {"tickets": list_support_tickets(limit=limit)}


def dashboard_appointments(*, start: str = "", end: str = "", status: str = "", limit: int = 500) -> dict[str, Any]:
    return {"appointments": list_appointments(start=start, end=end, status=status, limit=limit)}


def dashboard_appointment_create(body: AppointmentCreate) -> dict[str, Any]:
    label = body.requested_label.strip()
    if not label and body.requested_at.strip():
        label = body.requested_at.strip()
    appt = create_appointment(
        requested_at=body.requested_at,
        duration_min=body.duration_min,
        dealership_name=body.dealership_name,
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        phone=body.phone,
        reason=body.reason,
        status=body.status,
        source="manual",
        timezone=body.timezone,
        requested_label=label,
        notes=body.notes,
    )
    try:
        from support_ticket_slack import post_callback_scheduled_alert

        post_callback_scheduled_alert(
            dealership_name=appt["dealership_name"],
            contact_name=appt["contact_name"],
            phone=appt["phone"],
            email=appt["email"],
            when_label=appt["requested_label"] or appt["requested_at"],
            reason=appt["reason"],
            channel=appt["channel"],
            source="manual",
        )
    except Exception:
        pass
    return {"ok": True, "appointment": appt}


def dashboard_appointment_update(appointment_id: int, body: AppointmentUpdate) -> dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    appt = update_appointment(appointment_id, fields)
    if not appt:
        return {"ok": False, "error": "Appointment not found"}
    return {"ok": True, "appointment": appt}


def dashboard_appointment_delete(appointment_id: int) -> dict[str, Any]:
    ok = delete_appointment(appointment_id)
    return {"ok": ok}


def dashboard_cs_questions() -> dict[str, Any]:
    from cs_questions import get_cs_questions

    return get_cs_questions()


def dashboard_cs_questions_status() -> dict[str, Any]:
    from cs_questions import cs_questions_status

    return cs_questions_status()


def dashboard_cs_questions_rebuild() -> dict[str, Any]:
    from cs_questions import start_cs_questions_rebuild

    return start_cs_questions_rebuild()


class QaAnswerSave(BaseModel):
    key: str = ""
    question: str
    category: str = "other"
    answer: str = ""
    updated_by: str = ""


def dashboard_qa() -> dict[str, Any]:
    from support_qa import get_qa_board

    return get_qa_board()


def dashboard_qa_save(body: QaAnswerSave) -> dict[str, Any]:
    from support_qa import save_qa_answer

    return save_qa_answer(
        key=body.key,
        question=body.question,
        category=body.category,
        answer=body.answer,
        updated_by=body.updated_by,
    )


class QaGenerateRequest(BaseModel):
    scope: str = "unanswered"
    keys: list[str] = []


def dashboard_qa_generate(
    body: QaGenerateRequest,
    get_retriever: Any,
    get_tool_executor: Any,
) -> dict[str, Any]:
    from support_qa import start_qa_generation

    return start_qa_generation(
        get_retriever,
        get_tool_executor,
        scope=body.scope,
        keys=body.keys or None,
    )


def dashboard_qa_generate_status() -> dict[str, Any]:
    from support_qa import qa_generation_status

    return {"ok": True, **qa_generation_status()}


def dashboard_qa_generate_cancel() -> dict[str, Any]:
    from support_qa import cancel_qa_generation

    return cancel_qa_generation()


def dashboard_qa_approve_all(updated_by: str = "") -> dict[str, Any]:
    from support_qa import approve_all_qa_drafts

    return approve_all_qa_drafts(updated_by=updated_by)


class QaDiscardRequest(BaseModel):
    key: str


def dashboard_qa_discard(body: QaDiscardRequest) -> dict[str, Any]:
    from support_qa import discard_qa_draft

    return discard_qa_draft(key=body.key)


async def dashboard_qa_regenerate(
    body: QaAnswerSave,
    retriever: Any,
    executor: Any,
) -> dict[str, Any]:
    from support_qa import regenerate_qa_answer

    return await regenerate_qa_answer(
        retriever,
        executor,
        key=body.key,
        question=body.question,
        category=body.category,
    )


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


async def dashboard_hubspot_tickets_sync(*, full_backfill: bool = False) -> dict[str, Any]:
    return await run_hubspot_tickets_sync_async(full_backfill=full_backfill)
