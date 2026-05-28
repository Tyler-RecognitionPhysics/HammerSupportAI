"""Voice admin dashboard API — works locally and on production (Vercel)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

_CENTRAL_TZ = ZoneInfo("America/Chicago")

from fastapi import HTTPException
from pydantic import BaseModel, Field

import elevenlabs_admin
from voice_admin_auth import admin_auth_configured
from voice_call_outcomes import (
    call_needs_detail_enrichment,
    enrich_call_outcomes,
    normalize_call_duration,
    outcome_counts,
    primary_outcome,
)
from voice_dashboard_store import (
    SETTING_KEYS,
    _is_serverless,
    clear_settings,
    funnel_stats,
    funnel_stats_today_central,
    get_all_settings,
    get_call,
    list_active_sessions,
    list_calls,
    patch_call_outcomes,
    recent_events,
    session_log_to_transcript,
    set_settings,
)
from voice_dashboard_activity import enrich_activity_feed
from voice_instructions import clear_instruction_cache, get_default_prompts


class VoiceSettingsPatch(BaseModel):
    pen_prompt: str | None = None
    hammer_prompt: str | None = None
    pen_close_prompt: str | None = None
    chat_model: str | None = None


class AgentVoicePatch(BaseModel):
    voice_id: str = Field(..., min_length=1, max_length=80)


def _effective_settings() -> dict[str, Any]:
    defaults = get_default_prompts()
    overrides = get_all_settings()
    env_model = os.environ.get("ELEVENLABS_CHAT_MODEL", "gpt-4o-mini").strip()
    serverless = _is_serverless()
    # On production (Vercel) prompt overrides are *ignored* by the live voice agent —
    # the file-level prompts in this repo are the single source of truth so that the
    # dashboard UI never silently shadows a fresh deploy with a stale SQLite row.
    if serverless:
        effective_prompts = {
            "pen_prompt": defaults["pen_prompt"],
            "hammer_prompt": defaults["hammer_prompt"],
            "pen_close_prompt": defaults["pen_close_prompt"],
        }
    else:
        effective_prompts = {
            "pen_prompt": overrides.get("pen_prompt") or defaults["pen_prompt"],
            "hammer_prompt": overrides.get("hammer_prompt") or defaults["hammer_prompt"],
            "pen_close_prompt": overrides.get("pen_close_prompt") or defaults["pen_close_prompt"],
        }
    return {
        "defaults": defaults,
        "overrides": {k: overrides[k] for k in SETTING_KEYS if k in overrides},
        "effective": {
            **effective_prompts,
            "chat_model": overrides.get("chat_model") or env_model,
        },
        "has_overrides": bool(overrides),
        "prompts_editable": not serverless,
        "prompts_note": (
            "Prompt overrides persist on this machine only."
            if not serverless
            else (
                "Production prompts come from the repo files in demo/realtime-sales-demo/web/src/. "
                "Dashboard prompt edits are NOT used by the live voice AI on Vercel — edit the files "
                "and redeploy for changes to take effect. Use 'Reset' below to wipe any stale overrides."
            )
        ),
    }


def _merge_call_records(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = {**secondary, **primary}
    for key in (
        "capture_lead_fired",
        "agreement_email_sent",
        "i_approve_approved",
        "account_created",
        "pen_challenge_skipped",
        "pen_hammer_close_active",
    ):
        merged[key] = bool(primary.get(key) or secondary.get(key))
    values = dict(secondary.get("values") or {})
    values.update(primary.get("values") or {})
    merged["values"] = values
    if not merged.get("session_log"):
        merged["session_log"] = secondary.get("session_log") or []
    if not merged.get("transcript"):
        merged["transcript"] = secondary.get("transcript") or []
    merged["events"] = primary.get("events") or secondary.get("events") or []
    if not merged.get("interaction_summary"):
        merged["interaction_summary"] = secondary.get("interaction_summary") or ""
    if not merged.get("started_at"):
        merged["started_at"] = secondary.get("started_at") or ""
    if not merged.get("ended_at"):
        merged["ended_at"] = secondary.get("ended_at") or ""
    if not merged.get("duration_secs"):
        merged["duration_secs"] = primary.get("duration_secs") or secondary.get("duration_secs")
    if not merged.get("status"):
        merged["status"] = secondary.get("status") or primary.get("status") or ""
    return merged


def _stats_from_calls(calls: list[dict[str, Any]], *, period: str) -> dict[str, Any]:
    channels: dict[str, int] = {}
    agreements = approvals = accounts = pen_handoff = 0
    for call in calls:
        ch = call.get("channel") or "voice"
        channels[ch] = channels.get(ch, 0) + 1
        if call.get("agreement_email_sent") or call.get("capture_lead_fired"):
            agreements += 1
        if call.get("i_approve_approved"):
            approvals += 1
        if call.get("account_created"):
            accounts += 1
        if call.get("pen_hammer_close_active"):
            pen_handoff += 1
    return {
        "period": period,
        "calls_total": len(calls),
        "agreement_emails": agreements,
        "approvals": approvals,
        "accounts_created": accounts,
        "pen_to_hammer": pen_handoff,
        "channels": channels,
        "active_now": len(list_active_sessions()),
    }


def _call_occurred_at(call: dict[str, Any]) -> datetime | None:
    """When the call happened — started_at or ended_at only (not updated_at)."""
    for key in ("started_at", "ended_at"):
        raw = call.get(key)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except ValueError:
            continue
    return None


def _central_day_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Start/end of the current calendar day in US Central, as UTC datetimes."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(_CENTRAL_TZ)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _filter_calls_since(calls: list[dict[str, Any]], *, hours: int | None = None, days: int | None = None) -> list[dict[str, Any]]:
    if hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
    elif days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
    else:
        return calls
    out: list[dict[str, Any]] = []
    for call in calls:
        ts = _call_occurred_at(call)
        if ts is not None and ts >= since:
            out.append(call)
    return out


def _filter_calls_today_central(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls whose start/end falls on today's calendar date in US Central."""
    start, end = _central_day_bounds_utc()
    out: list[dict[str, Any]] = []
    for call in calls:
        ts = _call_occurred_at(call)
        if ts is not None and start <= ts < end:
            out.append(call)
    return out


async def _elevenlabs_calls(limit: int = 50) -> list[dict[str, Any]]:
    if not elevenlabs_admin.elevenlabs_configured():
        return []
    data = await elevenlabs_admin.list_conversations(page_size=min(limit, 100))
    return data.get("calls") or []


async def _elevenlabs_calls_for_stats(*, history_days: int = 8, max_calls: int = 300) -> list[dict[str, Any]]:
    """Paginate ElevenLabs conversations so overview stats are not capped at one page."""
    if not elevenlabs_admin.elevenlabs_configured():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(out) < max_calls:
        data = await elevenlabs_admin.list_conversations(page_size=100, cursor=cursor)
        batch = data.get("calls") or []
        if not batch:
            break
        out.extend(batch)
        oldest_ts: datetime | None = None
        for call in batch:
            ts = _call_occurred_at(call)
            if ts is not None and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
        if oldest_ts is not None and oldest_ts < cutoff and len(out) >= 50:
            break
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out


async def _live_calls_payload() -> list[dict[str, Any]]:
    """Active calls enriched with the latest transcript for the overview panel."""
    sessions = list_active_sessions()
    if not sessions:
        return []

    out: list[dict[str, Any]] = []
    for sess in sessions:
        call_id = str(sess.get("call_id") or "").strip()
        if not call_id:
            continue
        local = get_call(call_id) or {}
        values = dict(local.get("values") or {})
        values.update(sess.get("values") or {})

        transcript: list[dict[str, Any]] = list(sess.get("transcript") or [])
        if not transcript:
            transcript = list(local.get("transcript") or [])
        if not transcript:
            transcript = session_log_to_transcript(local.get("session_log") or [])

        if not transcript and elevenlabs_admin.elevenlabs_configured():
            try:
                remote = await elevenlabs_admin.get_conversation(call_id)
                transcript = list(remote.get("transcript") or [])
                if not transcript:
                    transcript = session_log_to_transcript(remote.get("session_log") or [])
                values.update(remote.get("values") or {})
            except HTTPException:
                pass
            except Exception:
                pass

        out.append(
            {
                "call_id": call_id,
                "started_at": sess.get("started_at") or local.get("started_at") or "",
                "scenario": sess.get("scenario") or values.get("voice_scenario") or "",
                "channel": sess.get("channel") or local.get("channel") or "voice",
                "values": values,
                "transcript": transcript[-50:],
                "session_log": local.get("session_log") or [],
            }
        )
    return out


def _merge_call_lists(local: list[dict[str, Any]], remote: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {c["call_id"]: c for c in remote if c.get("call_id")}
    for call in local:
        cid = call.get("call_id")
        if not cid:
            continue
        if cid in by_id:
            by_id[cid] = _merge_call_records(call, by_id[cid])
        else:
            by_id[cid] = call
    merged = list(by_id.values())
    merged.sort(
        key=lambda c: c.get("ended_at") or c.get("started_at") or "",
        reverse=True,
    )
    return merged


async def _merged_calls(limit: int = 50) -> list[dict[str, Any]]:
    local = list_calls(limit=limit)
    if not elevenlabs_admin.elevenlabs_configured():
        return local
    try:
        remote = await _elevenlabs_calls(limit=limit)
    except HTTPException:
        return local
    return _merge_call_lists(local, remote)[:limit]


async def _merged_calls_for_stats() -> list[dict[str, Any]]:
    """Broader merge for overview funnel — paginated ElevenLabs + local store."""
    local = list_calls(limit=500)
    if not elevenlabs_admin.elevenlabs_configured():
        return local
    try:
        remote = await _elevenlabs_calls_for_stats()
    except HTTPException:
        return local
    return _merge_call_lists(local, remote)


async def dashboard_overview() -> dict[str, Any]:
    from agreement_approvals import _load_store

    approvals = _load_store()
    approved_count = len(approvals.get("approved") or {})

    if elevenlabs_admin.elevenlabs_configured():
        calls = await _merged_calls_for_stats()
        funnel = _stats_from_calls(_filter_calls_since(calls, days=7), period="last_7d")
        funnel_today = _stats_from_calls(_filter_calls_today_central(calls), period="today_central")
        data_source = "elevenlabs+local"
    else:
        funnel = funnel_stats(days=7)
        funnel_today = funnel_stats_today_central()
        calls = []
        data_source = "local"

    return {
        "funnel": funnel,
        "funnel_today": funnel_today,
        "active_sessions": list_active_sessions(),
        "live_calls": await _live_calls_payload(),
        "recent_events": enrich_activity_feed(recent_events(limit=60), limit=20),
        "approvals_count": approved_count,
        "elevenlabs_configured": elevenlabs_admin.elevenlabs_configured(),
        "admin_configured": admin_auth_configured(),
        "data_source": data_source,
        "environment": "production" if _is_serverless() else "local",
    }


def dashboard_settings_get() -> dict[str, Any]:
    return _effective_settings()


def dashboard_settings_patch(body: VoiceSettingsPatch) -> dict[str, Any]:
    if _is_serverless() and any(
        v is not None for v in (body.pen_prompt, body.hammer_prompt, body.pen_close_prompt)
    ):
        raise HTTPException(
            400,
            "Prompt overrides are not persisted on production. Edit prompt files in the repo and redeploy.",
        )
    updates: dict[str, Any] = {}
    if body.pen_prompt is not None:
        updates["pen_prompt"] = body.pen_prompt
    if body.hammer_prompt is not None:
        updates["hammer_prompt"] = body.hammer_prompt
    if body.pen_close_prompt is not None:
        updates["pen_close_prompt"] = body.pen_close_prompt
    if body.chat_model is not None:
        model = body.chat_model.strip()
        if not model:
            raise HTTPException(400, "chat_model cannot be empty")
        if _is_serverless():
            raise HTTPException(
                400,
                "Set ELEVENLABS_CHAT_MODEL in Vercel environment variables for production model changes.",
            )
        updates["chat_model"] = model
    if not updates:
        raise HTTPException(400, "No settings provided")
    set_settings(updates)
    clear_instruction_cache()
    return {"ok": True, "settings": _effective_settings()}


def dashboard_settings_reset() -> dict[str, Any]:
    """Wipe all dashboard prompt overrides so the file-level prompts are authoritative.

    Allowed in serverless mode too — the dashboard SQLite lives in /tmp and a stale
    override from a previous editing session could shadow the deployed prompt files
    until the next cold start. This lets an admin force-clear at will.
    """
    clear_settings()
    clear_instruction_cache()
    return {"ok": True, "settings": _effective_settings()}


async def dashboard_elevenlabs_voices() -> dict[str, Any]:
    voices = await elevenlabs_admin.list_voices()
    return {"voices": voices}


async def dashboard_elevenlabs_agent() -> dict[str, Any]:
    agent = await elevenlabs_admin.get_agent()
    return {"agent": agent}


async def dashboard_elevenlabs_agent_patch(body: AgentVoicePatch) -> dict[str, Any]:
    result = await elevenlabs_admin.update_agent_voice(body.voice_id)
    return result


_DETAIL_FETCH_LIMIT = 25
_DETAIL_FETCH_CONCURRENCY = 6


async def _enrich_one_call_from_detail(call: dict[str, Any]) -> dict[str, Any]:
    """Fetch full ElevenLabs conversation transcript and merge funnel flags."""
    call_id = str(call.get("call_id") or "").strip()
    if not call_id:
        return call
    try:
        remote = await elevenlabs_admin.get_conversation(call_id)
        merged = _merge_call_records(call, remote)
        enrich_call_outcomes(merged)
        normalize_call_duration(merged)
        merged["primary_outcome"] = primary_outcome(merged)
        try:
            patch_call_outcomes(call_id, merged)
        except Exception:
            pass
        return merged
    except HTTPException as exc:
        if exc.status_code != 404:
            pass
    except Exception:
        pass
    return call


async def _enrich_calls_with_details(calls: list[dict[str, Any]]) -> None:
    """Fill journey flags from ElevenLabs transcripts so the calls table is accurate on load."""
    for call in calls:
        enrich_call_outcomes(call)
        normalize_call_duration(call)

    if not elevenlabs_admin.elevenlabs_configured():
        for call in calls:
            call["primary_outcome"] = primary_outcome(call)
        return

    indices = [i for i, call in enumerate(calls) if call_needs_detail_enrichment(call)]
    indices = indices[:_DETAIL_FETCH_LIMIT]
    if not indices:
        for call in calls:
            call["primary_outcome"] = primary_outcome(call)
        return

    sem = asyncio.Semaphore(_DETAIL_FETCH_CONCURRENCY)

    async def _fetch(idx: int) -> tuple[int, dict[str, Any]]:
        async with sem:
            enriched = await _enrich_one_call_from_detail(calls[idx])
            return idx, enriched

    results = await asyncio.gather(*[_fetch(i) for i in indices], return_exceptions=True)
    for item in results:
        if isinstance(item, BaseException):
            continue
        idx, enriched = item
        calls[idx] = enriched

    for call in calls:
        if "primary_outcome" not in call:
            call["primary_outcome"] = primary_outcome(call)


async def dashboard_calls(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    calls = await _merged_calls(limit=min(limit + offset, 100))
    if offset:
        calls = calls[offset:]
    calls = calls[:limit]
    await _enrich_calls_with_details(calls)
    return {"calls": calls, "outcome_counts": outcome_counts(calls)}


async def dashboard_call_detail(call_id: str) -> dict[str, Any]:
    local = get_call(call_id)
    if elevenlabs_admin.elevenlabs_configured():
        try:
            remote = await elevenlabs_admin.get_conversation(call_id)
            if local:
                call = _merge_call_records(local, remote)
            else:
                call = remote
            enrich_call_outcomes(call)
            call["primary_outcome"] = primary_outcome(call)
            # Write any newly discovered funnel flags back to the local DB so
            # the calls list table shows the correct outcome without a re-fetch.
            try:
                patch_call_outcomes(call_id, call)
            except Exception:
                pass
            return {"call": call}
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    if local:
        enrich_call_outcomes(local)
        local["primary_outcome"] = primary_outcome(local)
        try:
            patch_call_outcomes(call_id, local)
        except Exception:
            pass
        return {"call": local}
    raise HTTPException(404, "Call not found")


def dashboard_calendar(*, days: int = 14) -> dict[str, Any]:
    from google_calendar import list_upcoming_events

    return list_upcoming_events(days=days)
