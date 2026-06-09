"""Fetch HubSpot Help Desk activities (emails, notes, calls, etc.) for ticket sync."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx

from hubspot_budget import consume as _consume_hubspot_budget

_log = logging.getLogger(__name__)

_HUBSPOT_API = "https://api.hubapi.com"
_TAG_RE = re.compile(r"<[^>]+>")

# CRM activity object types linked to tickets.
_ACTIVITY_TYPES: dict[str, list[str]] = {
    "notes": [
        "hs_note_body",
        "hs_timestamp",
        "hs_createdate",
        "hs_lastmodifieddate",
    ],
    "emails": [
        "hs_email_subject",
        "hs_email_text",
        "hs_email_html",
        "hs_email_direction",
        "hs_email_status",
        "hs_timestamp",
        "hs_createdate",
    ],
    "calls": [
        "hs_call_title",
        "hs_call_body",
        "hs_call_summary",
        "hs_call_duration",
        "hs_call_status",
        "hs_call_direction",
        "hs_call_recording_url",
        "hs_call_transcription_id",
        "hs_timestamp",
        "hs_createdate",
    ],
    "tasks": [
        "hs_task_subject",
        "hs_task_body",
        "hs_task_status",
        "hs_task_priority",
        "hs_task_type",
        "hs_timestamp",
        "hs_createdate",
    ],
    "meetings": [
        "hs_meeting_title",
        "hs_meeting_body",
        "hs_internal_meeting_notes",
        "hs_meeting_start_time",
        "hs_meeting_end_time",
        "hs_timestamp",
        "hs_createdate",
    ],
    "communications": [
        "hs_communication_body",
        "hs_communication_channel_type",
        "hs_communication_logged_from",
        "hs_timestamp",
        "hs_createdate",
    ],
}

_ASSOCIATION_BATCH_SIZE = 100
_OBJECT_BATCH_SIZE = 100
_CONVERSATION_CONCURRENCY = 4


@dataclass
class TimelineEntry:
    kind: str
    timestamp: str
    title: str
    body: str
    meta: dict[str, str] = field(default_factory=dict)


def _html_to_text(raw: str) -> str:
    if not raw or "<" not in raw:
        return (raw or "").strip()
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r" +", " ", text)
    return text.strip()


def _parse_timestamp(value: str) -> datetime:
    if not value:
        return datetime.min
    raw = str(value).strip()
    try:
        if raw.isdigit():
            ms = int(raw)
            if ms > 1_000_000_000_000:
                return datetime.fromtimestamp(ms / 1000.0)
            return datetime.fromtimestamp(float(ms))
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.min


def _prop(props: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = props.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


async def _hubspot_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    retries: int = 3,
) -> httpx.Response | None:
    for attempt in range(retries):
        # Count every real request against the support AI's daily HubSpot budget.
        # Raises HubSpotBudgetExceeded (propagated to stop the sync) when over cap.
        _consume_hubspot_budget(1)
        try:
            resp = await client.request(method, url, headers=headers, json=json_body, params=params)
            if resp.status_code == 429:
                wait = min(2 ** attempt, 8)
                _log.warning("HubSpot rate limited; sleeping %ss", wait)
                await asyncio.sleep(wait)
                continue
            return resp
        except httpx.HTTPError as exc:
            if attempt == retries - 1:
                _log.warning("HubSpot request failed %s %s: %s", method, url, exc)
                return None
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _batch_read_associations(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    ticket_ids: list[str],
    to_object_type: str,
) -> dict[str, list[str]]:
    """Return ticket_id -> list of associated object IDs."""
    out: dict[str, list[str]] = {tid: [] for tid in ticket_ids}
    if not ticket_ids:
        return out

    # Pending inputs may need pagination via `after` cursor per ticket.
    pending: dict[str, str | None] = {tid: None for tid in ticket_ids}

    while pending:
        inputs = [{"id": tid, **({"after": after} if after else {})} for tid, after in pending.items()]
        pending = {}

        for offset in range(0, len(inputs), _ASSOCIATION_BATCH_SIZE):
            chunk = inputs[offset : offset + _ASSOCIATION_BATCH_SIZE]
            resp = await _hubspot_request(
                client,
                "POST",
                f"{_HUBSPOT_API}/crm/v4/associations/tickets/{to_object_type}/batch/read",
                headers=headers,
                json_body={"inputs": chunk},
            )
            if resp is None:
                continue
            if resp.status_code in (401, 403):
                _log.warning(
                    "HubSpot associations %s forbidden (%s) — check private app scopes",
                    to_object_type,
                    resp.status_code,
                )
                return out
            if resp.status_code >= 400:
                # Fallback to 2026-03 associations path.
                resp = await _hubspot_request(
                    client,
                    "POST",
                    f"{_HUBSPOT_API}/crm/associations/2026-03/tickets/{to_object_type}/batch/read",
                    headers=headers,
                    json_body={"inputs": chunk},
                )
                if resp is None or resp.status_code >= 400:
                    _log.warning(
                        "HubSpot associations batch read failed for %s: %s",
                        to_object_type,
                        resp.status_code if resp else "no response",
                    )
                    continue

            payload = resp.json()
            for row in payload.get("results") or []:
                from_obj = row.get("from") or {}
                tid = str(from_obj.get("id") or "").strip()
                if not tid:
                    continue
                for assoc in row.get("to") or []:
                    obj_id = str(assoc.get("toObjectId") or assoc.get("id") or "").strip()
                    if obj_id:
                        out.setdefault(tid, []).append(obj_id)

                paging = row.get("paging") or {}
                next_after = (paging.get("next") or {}).get("after")
                if next_after:
                    pending[tid] = str(next_after)

            await asyncio.sleep(0.15)

    # Dedupe while preserving order.
    for tid, ids in list(out.items()):
        seen: set[str] = set()
        deduped: list[str] = []
        for obj_id in ids:
            if obj_id not in seen:
                seen.add(obj_id)
                deduped.append(obj_id)
        out[tid] = deduped
    return out


async def _batch_read_objects(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    object_type: str,
    object_ids: list[str],
    properties: list[str],
) -> dict[str, dict[str, Any]]:
    """Return object_id -> properties dict."""
    out: dict[str, dict[str, Any]] = {}
    if not object_ids:
        return out

    unique_ids = list(dict.fromkeys(object_ids))
    for offset in range(0, len(unique_ids), _OBJECT_BATCH_SIZE):
        chunk = unique_ids[offset : offset + _OBJECT_BATCH_SIZE]
        resp = await _hubspot_request(
            client,
            "POST",
            f"{_HUBSPOT_API}/crm/v3/objects/{object_type}/batch/read",
            headers=headers,
            json_body={
                "properties": properties,
                "inputs": [{"id": obj_id} for obj_id in chunk],
            },
        )
        if resp is None:
            continue
        if resp.status_code in (401, 403):
            _log.warning("HubSpot %s batch read forbidden (%s)", object_type, resp.status_code)
            return out
        if resp.status_code >= 400:
            _log.warning("HubSpot %s batch read failed: %s", object_type, resp.status_code)
            continue

        for row in resp.json().get("results") or []:
            obj_id = str(row.get("id") or "").strip()
            if obj_id:
                out[obj_id] = row.get("properties") or {}
        await asyncio.sleep(0.15)
    return out


async def _fetch_transcript_text(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    transcript_id: str,
) -> str:
    if not transcript_id:
        return ""
    for path in (
        f"{_HUBSPOT_API}/crm/v3/extensions/calling/transcripts/{transcript_id}",
        f"{_HUBSPOT_API}/crm/extensions/calling/2026-03/transcripts/{transcript_id}",
    ):
        resp = await _hubspot_request(client, "GET", path, headers=headers)
        if resp is None or resp.status_code >= 400:
            continue
        data = resp.json()
        utterances = data.get("transcriptUtterances") or []
        lines: list[str] = []
        for utt in utterances:
            speaker = (utt.get("speaker") or {}).get("name") or "Speaker"
            text = str(utt.get("text") or "").strip()
            if text:
                lines.append(f"{speaker}: {text}")
        if lines:
            return "\n".join(lines)
    return ""


async def _fetch_call_transcripts(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    call_props_by_id: dict[str, dict[str, Any]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    tasks: list[tuple[str, str]] = []
    for call_id, props in call_props_by_id.items():
        transcript_id = _prop(props, "hs_call_transcription_id")
        if transcript_id:
            tasks.append((call_id, transcript_id))

    for call_id, transcript_id in tasks:
        text = await _fetch_transcript_text(client, headers, transcript_id)
        if text:
            out[call_id] = text
        await asyncio.sleep(0.1)
    return out


async def _fetch_conversation_messages(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    ticket_id: str,
) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    resp = await _hubspot_request(
        client,
        "GET",
        f"{_HUBSPOT_API}/conversations/v3/conversations/threads",
        headers=headers,
        params={"associatedTicketId": ticket_id, "limit": 50},
    )
    if resp is None or resp.status_code >= 400:
        if resp and resp.status_code in (401, 403):
            _log.debug("Conversations API unavailable for ticket %s (%s)", ticket_id, resp.status_code)
        return entries

    threads = resp.json().get("results") or []
    for thread in threads:
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            continue

        msg_resp = await _hubspot_request(
            client,
            "GET",
            f"{_HUBSPOT_API}/conversations/v3/conversations/threads/{thread_id}/messages",
            headers=headers,
            params={"limit": 100},
        )
        if msg_resp is None or msg_resp.status_code >= 400:
            continue

        for msg in msg_resp.json().get("results") or []:
            msg_type = str(msg.get("type") or "MESSAGE").upper()
            if msg_type == "WELCOME_MESSAGE":
                continue

            created = str(msg.get("createdAt") or msg.get("updatedAt") or "")
            text = str(msg.get("text") or "").strip()
            rich = msg.get("richText") or {}
            if not text and isinstance(rich, dict):
                text = str(rich.get("text") or rich.get("html") or "").strip()
            if not text:
                text = _html_to_text(str(msg.get("html") or ""))

            subject = str(msg.get("subject") or "").strip()
            direction = str(msg.get("direction") or msg.get("channelType") or "").strip()
            senders = msg.get("senders") or msg.get("sender") or []
            sender_name = ""
            if isinstance(senders, list) and senders:
                sender_name = str((senders[0] or {}).get("name") or "").strip()
            elif isinstance(senders, dict):
                sender_name = str(senders.get("name") or "").strip()

            title_bits = [msg_type.title().replace("_", " ")]
            if direction:
                title_bits.append(direction.title())
            if sender_name:
                title_bits.append(sender_name)
            if subject:
                title_bits.append(subject)

            entries.append(
                TimelineEntry(
                    kind="conversation",
                    timestamp=created,
                    title=" — ".join(title_bits),
                    body=text,
                    meta={"thread_id": thread_id, "message_type": msg_type},
                )
            )
        await asyncio.sleep(0.1)
    return entries


def _activity_to_entry(
    object_type: str,
    obj_id: str,
    props: dict[str, Any],
    *,
    transcript: str = "",
) -> TimelineEntry | None:
    timestamp = _prop(props, "hs_timestamp", "hs_createdate", "hs_meeting_start_time")
    meta: dict[str, str] = {"object_id": obj_id}

    if object_type == "notes":
        body = _html_to_text(_prop(props, "hs_note_body"))
        if not body:
            return None
        return TimelineEntry(kind="note", timestamp=timestamp, title="Note", body=body, meta=meta)

    if object_type == "emails":
        subject = _prop(props, "hs_email_subject") or "Email"
        body = _html_to_text(_prop(props, "hs_email_text", "hs_email_html"))
        direction = _prop(props, "hs_email_direction")
        status = _prop(props, "hs_email_status")
        title = subject
        if direction:
            title = f"{direction.title()} — {subject}"
        if status:
            meta["status"] = status
        if not body:
            return None
        return TimelineEntry(kind="email", timestamp=timestamp, title=title, body=body, meta=meta)

    if object_type == "calls":
        title = _prop(props, "hs_call_title") or "Call"
        body_parts: list[str] = []
        summary = _html_to_text(_prop(props, "hs_call_summary"))
        call_body = _html_to_text(_prop(props, "hs_call_body"))
        if summary:
            body_parts.append(f"Summary: {summary}")
        if call_body:
            body_parts.append(call_body)
        if transcript:
            body_parts.append(f"Transcript:\n{transcript}")
        duration = _prop(props, "hs_call_duration")
        status = _prop(props, "hs_call_status")
        direction = _prop(props, "hs_call_direction")
        if duration:
            meta["duration_ms"] = duration
        if status:
            meta["status"] = status
        if direction:
            meta["direction"] = direction
        recording = _prop(props, "hs_call_recording_url")
        if recording:
            meta["recording_url"] = "[recording-redacted]"
        if not body_parts:
            return None
        return TimelineEntry(
            kind="call",
            timestamp=timestamp,
            title=title,
            body="\n\n".join(body_parts),
            meta=meta,
        )

    if object_type == "tasks":
        subject = _prop(props, "hs_task_subject") or "Task"
        body = _html_to_text(_prop(props, "hs_task_body"))
        status = _prop(props, "hs_task_status")
        if status:
            meta["status"] = status
        if not body:
            return None
        return TimelineEntry(kind="task", timestamp=timestamp, title=subject, body=body, meta=meta)

    if object_type == "meetings":
        title = _prop(props, "hs_meeting_title") or "Meeting"
        body_parts = []
        meeting_body = _html_to_text(_prop(props, "hs_meeting_body"))
        internal = _html_to_text(_prop(props, "hs_internal_meeting_notes"))
        if meeting_body:
            body_parts.append(meeting_body)
        if internal:
            body_parts.append(f"Internal notes: {internal}")
        if not body_parts:
            return None
        return TimelineEntry(
            kind="meeting",
            timestamp=timestamp,
            title=title,
            body="\n\n".join(body_parts),
            meta=meta,
        )

    if object_type == "communications":
        channel = _prop(props, "hs_communication_channel_type") or "Message"
        body = _html_to_text(_prop(props, "hs_communication_body"))
        if not body:
            return None
        return TimelineEntry(
            kind="communication",
            timestamp=timestamp,
            title=channel.title(),
            body=body,
            meta=meta,
        )

    return None


def render_timeline_markdown(entries: list[TimelineEntry]) -> str:
    if not entries:
        return ""

    sorted_entries = sorted(entries, key=lambda e: _parse_timestamp(e.timestamp))
    lines = ["", "## Help Desk Timeline", ""]
    for entry in sorted_entries:
        ts = entry.timestamp[:19].replace("T", " ") if entry.timestamp else "Unknown time"
        lines.append(f"### {ts} — {entry.kind.title()}: {entry.title}")
        if entry.meta.get("status"):
            lines.append(f"- Status: {entry.meta['status']}")
        if entry.meta.get("direction"):
            lines.append(f"- Direction: {entry.meta['direction']}")
        lines.append("")
        lines.append(entry.body.strip())
        lines.append("")
    return "\n".join(lines).rstrip()


async def fetch_ticket_timelines(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    ticket_ids: list[str],
    *,
    redact: Callable[[str], str] | None = None,
    include_conversations: bool = True,
) -> dict[str, list[TimelineEntry]]:
    """Fetch full activity timelines for a batch of ticket IDs."""
    if not ticket_ids:
        return {}

    timelines: dict[str, list[TimelineEntry]] = {tid: [] for tid in ticket_ids}

    # 1) Batch-read CRM activity associations per type.
    assoc_by_type: dict[str, dict[str, list[str]]] = {}
    for object_type in _ACTIVITY_TYPES:
        assoc_by_type[object_type] = await _batch_read_associations(
            client, headers, ticket_ids, object_type
        )

    # 2) Batch-read activity objects and map back to tickets.
    for object_type, properties in _ACTIVITY_TYPES.items():
        ticket_to_ids = assoc_by_type.get(object_type) or {}
        all_ids: list[str] = []
        for ids in ticket_to_ids.values():
            all_ids.extend(ids)
        if not all_ids:
            continue

        props_by_id = await _batch_read_objects(client, headers, object_type, all_ids, properties)
        transcripts_by_call: dict[str, str] = {}
        if object_type == "calls":
            transcripts_by_call = await _fetch_call_transcripts(client, headers, props_by_id)

        for ticket_id, obj_ids in ticket_to_ids.items():
            for obj_id in obj_ids:
                props = props_by_id.get(obj_id)
                if not props:
                    continue
                transcript = transcripts_by_call.get(obj_id, "") if object_type == "calls" else ""
                entry = _activity_to_entry(object_type, obj_id, props, transcript=transcript)
                if entry:
                    if redact:
                        entry = TimelineEntry(
                            kind=entry.kind,
                            timestamp=entry.timestamp,
                            title=redact(entry.title),
                            body=redact(entry.body),
                            meta=entry.meta,
                        )
                    timelines[ticket_id].append(entry)

    # 3) Help Desk conversation threads (emails/chats in inbox UI).
    if include_conversations:
        sem = asyncio.Semaphore(_CONVERSATION_CONCURRENCY)

        async def _one(ticket_id: str) -> None:
            async with sem:
                conv_entries = await _fetch_conversation_messages(client, headers, ticket_id)
                if redact:
                    conv_entries = [
                        TimelineEntry(
                            kind=e.kind,
                            timestamp=e.timestamp,
                            title=redact(e.title),
                            body=redact(e.body),
                            meta=e.meta,
                        )
                        for e in conv_entries
                    ]
                timelines[ticket_id].extend(conv_entries)

        await asyncio.gather(*[_one(tid) for tid in ticket_ids])

    return timelines
