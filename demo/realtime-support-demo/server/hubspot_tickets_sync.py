"""Sync HubSpot Resolved Tickets into raw/support-data/hubspot-tickets/."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from hubspot_ticket_activities import (
    TimelineEntry,
    fetch_ticket_timelines,
    render_timeline_markdown,
)
from hubspot_budget import (
    HubSpotBudgetExceeded,
    consume as _consume_hubspot_budget,
    remaining_today as _hubspot_budget_remaining,
)

_log = logging.getLogger(__name__)

_TICKET_PROCESS_BATCH = 50
_SYNC_SCHEMA_VERSION = 2

# Live progress for the currently running (or most recent) ticket sync/backfill.
# Kept in-process; the persistent host runs the background task in this same process.
_PROGRESS: dict[str, Any] = {
    "running": False,
    "phase": "idle",  # idle | discovering | enriching | indexing | done | error
    "full_backfill": False,
    "discovered": 0,
    "closed_candidates": 0,
    "processed": 0,
    "written": 0,
    "started_at": "",
    "updated_at": "",
    "message": "",
    "error": "",
}


def _reset_progress(full_backfill: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _PROGRESS.update(
        {
            "running": True,
            "phase": "discovering",
            "full_backfill": bool(full_backfill),
            "discovered": 0,
            "closed_candidates": 0,
            "processed": 0,
            "written": 0,
            "started_at": now,
            "updated_at": now,
            "message": "Discovering tickets in HubSpot…",
            "error": "",
        }
    )


def _set_progress(**kw: Any) -> None:
    kw["updated_at"] = datetime.now(timezone.utc).isoformat()
    _PROGRESS.update(kw)

_HUBSPOT_API = "https://api.hubapi.com"
_RAW_SUBDIR = "hubspot-tickets"

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.I)


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _hubspot_token() -> str:
    return (
        os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
        or os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    )


def _portal_id() -> str:
    return os.environ.get("HUBSPOT_PORTAL_ID", "3355079").strip()


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _sync_data_dir() -> Path:
    override = os.environ.get("SUPPORT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-support-demo")
    return _repo_root() / "knowledge_support" / "data"


def _state_db_path() -> Path:
    override = os.environ.get("SUPPORT_HUBSPOT_TICKETS_STATE_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _sync_data_dir() / "hubspot_tickets_sync.sqlite"


def _raw_support_dir() -> Path:
    override = os.environ.get("SUPPORT_RAW_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / "raw" / "support-data"


def _raw_hubspot_tickets_dir() -> Path:
    return _raw_support_dir() / _RAW_SUBDIR


def _kb_db_path() -> Path:
    override = os.environ.get("SUPPORT_KB_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _sync_data_dir() / "support_kb.sqlite"


def _redact_pii(text: str) -> str:
    if not text:
        return ""
    text = _EMAIL_RE.sub("[email-redacted]", text)
    text = _PHONE_RE.sub("[phone-redacted]", text)
    text = _VIN_RE.sub("[vin-redacted]", text)
    return text


def _init_state_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hubspot_tickets_sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_sync_at TEXT NOT NULL DEFAULT '',
            ticket_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            schema_version INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO hubspot_tickets_sync_state (id) VALUES (1);
        CREATE TABLE IF NOT EXISTS hubspot_tickets (
            ticket_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL DEFAULT '',
            stage_id TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            engagements_hash TEXT NOT NULL DEFAULT '',
            file_name TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    try:
        conn.execute(
            "ALTER TABLE hubspot_tickets_sync_state ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE hubspot_tickets ADD COLUMN engagements_hash TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass


def _rebuild_kb_index() -> None:
    import sys

    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from knowledge_support.scripts.sync_sqlite import sync as rebuild_index

    kb_path = _kb_db_path()
    kb_path.parent.mkdir(parents=True, exist_ok=True)
    rebuild_index(
        root / "wiki-support",
        kb_path,
        support_raw_dir=_raw_support_dir(),
        chunk_size=1200,
        chunk_overlap=150,
        full_wiki=False,
    )

    try:
        from knowledge_support.kb_artifact import write_manifest

        files_on_disk = len(list(_raw_hubspot_tickets_dir().glob("*.md")))
        write_manifest(ticket_count=files_on_disk)
    except Exception:
        _log.exception("manifest write failed after kb rebuild")


def _write_ticket_markdown(
    dest: Path,
    *,
    ticket_id: str,
    subject: str,
    content: str,
    created_at: str,
    updated_at: str,
    stage_id: str,
    category: str,
    priority: str,
    custom_props: dict[str, Any],
    timeline: list[TimelineEntry] | None = None,
) -> tuple[str, str]:
    lines = [
        "---",
        f"ticket_id: {ticket_id}",
        "source: hubspot-tickets",
        f"created_at: {created_at}",
        f"updated_at: {updated_at}",
        f"stage: {stage_id}",
    ]
    if category:
        lines.append(f"category: {category}")
    if priority:
        lines.append(f"priority: {priority}")

    # Add other custom properties to frontmatter (except the core ones)
    core_keys = {
        "subject", "content", "hs_pipeline_stage", "hs_ticket_category",
        "hs_ticket_priority", "createdate", "hs_lastmodifieddate"
    }
    for k, v in custom_props.items():
        if v is not None and k not in core_keys:
            # Clean newlines from frontmatter values
            clean_v = str(v).replace("\n", " ").strip()
            lines.append(f"{k}: {clean_v}")

    lines.extend([
        "---",
        "",
        f"# {subject}",
        "",
        "## Description",
        content,
    ])

    # Append resolution/close notes if populated
    res_keys = ["resolution", "hs_resolution", "close_notes", "hs_close_notes"]
    for rk in res_keys:
        rv = custom_props.get(rk)
        if isinstance(rv, str) and rv.strip():
            lines.extend([
                "",
                f"## Resolution ({rk.replace('_', ' ').title()})",
                rv.strip(),
            ])

    timeline_md = render_timeline_markdown(timeline or [])
    if timeline_md:
        lines.append(timeline_md)

    body_text = "\n".join(lines).strip() + "\n"
    dest.write_text(body_text, encoding="utf-8")
    content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    engagements_hash = hashlib.sha256(timeline_md.encode("utf-8")).hexdigest() if timeline_md else ""
    return content_hash, engagements_hash


def hubspot_tickets_sync_status() -> dict[str, Any]:
    token = _hubspot_token()
    state_path = _state_db_path()
    raw_dir = _raw_hubspot_tickets_dir()

    out: dict[str, Any] = {
        "configured": bool(token),
        "portal_id": _portal_id(),
        "raw_dir": str(raw_dir),
        "sync_schema_version": _SYNC_SCHEMA_VERSION,
        "enrichment": "full",
        "running": bool(_sync_running) or bool(_PROGRESS.get("running")),
        "progress": dict(_PROGRESS),
    }

    try:
        from hubspot_budget import snapshot as _hubspot_budget_snapshot

        out["hubspot_budget"] = _hubspot_budget_snapshot()
    except Exception:
        pass

    if not state_path.is_file():
        out.update({"last_sync_at": "", "ticket_count": 0, "last_error": ""})
        out["files_on_disk"] = 0
        return out

    conn = sqlite3.connect(str(state_path))
    try:
        _init_state_db(conn)
        row = conn.execute(
            "SELECT last_sync_at, ticket_count, last_error FROM hubspot_tickets_sync_state WHERE id = 1"
        ).fetchone()
        if row:
            out["last_sync_at"] = row[0]
            out["ticket_count"] = row[1]
            out["last_error"] = row[2]
    except Exception as exc:
        _log.warning("Failed to read tickets sync state from SQLite: %s", exc)
        out.update({"last_sync_at": "", "ticket_count": 0, "last_error": str(exc)})
    finally:
        conn.close()

    out["files_on_disk"] = len(list(raw_dir.glob("*.md"))) if raw_dir.is_dir() else 0
    out["indexed_tickets"] = max(int(out.get("ticket_count") or 0), out["files_on_disk"])

    if _is_serverless():
        try:
            from knowledge_support.kb_artifact import kb_db_path, read_manifest

            manifest = read_manifest()
            if manifest and manifest.get("ticket_count"):
                out["indexed_tickets"] = int(manifest["ticket_count"])
            elif kb_db_path().is_file():
                from knowledge_support.kb_artifact import _sqlite_stats

                out["indexed_tickets"] = _sqlite_stats(kb_db_path()).get("ticket_docs", out["indexed_tickets"])
        except Exception:
            pass

    return out


async def _get_closed_stage_ids(client: httpx.AsyncClient, headers: dict[str, str]) -> set[str]:
    # Check if user specified closed stage IDs in env
    env_closed = os.environ.get("HUBSPOT_CLOSED_STAGE_IDS", "").strip()
    if env_closed:
        return {s.strip() for s in env_closed.split(",") if s.strip()}

    # Otherwise, query ticket pipelines
    closed_stages: set[str] = set()
    try:
        _consume_hubspot_budget(1)
        resp = await client.get(f"{_HUBSPOT_API}/crm/v3/pipelines/tickets", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            for pipeline in data.get("results") or []:
                for stage in pipeline.get("stages") or []:
                    metadata = stage.get("metadata") or {}
                    if metadata.get("ticketState") == "CLOSED":
                        closed_stages.add(stage.get("id"))
    except Exception as exc:
        _log.warning("Could not fetch ticket pipelines via API: %s", exc)

    # Standard fallback stage IDs/labels for default HubSpot ticket pipeline (Closed is typically "4" or "closed")
    if not closed_stages:
        closed_stages = {"4", "closed", "resolved"}

    return closed_stages


async def run_hubspot_tickets_sync_async(*, full_backfill: bool = False) -> dict[str, Any]:
    token = _hubspot_token()
    if not token:
        return {
            "ok": False,
            "error": "Set HUBSPOT_PRIVATE_APP_TOKEN in server/.env",
        }

    # Respect the support AI's hard daily HubSpot budget. If it's already spent,
    # don't start a sync — it would only stop almost immediately anyway.
    if _hubspot_budget_remaining() <= 0:
        msg = "Daily HubSpot API budget reached — sync paused until tomorrow (UTC)."
        _set_progress(phase="idle", running=False, message=msg, error="")
        _log.warning("HubSpot tickets sync skipped: %s", msg)
        return {"ok": False, "budget_paused": True, "error": msg}

    _reset_progress(full_backfill)

    headers = {"Authorization": f"Bearer {token}"}
    raw_dir = _raw_hubspot_tickets_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)

    state_path = _state_db_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine if last sync exists and check if we are doing incremental or full
    last_sync_at = ""
    stored_schema = 0
    conn = sqlite3.connect(str(state_path))
    try:
        _init_state_db(conn)
        row = conn.execute(
            "SELECT last_sync_at, schema_version FROM hubspot_tickets_sync_state WHERE id = 1"
        ).fetchone()
        if row:
            last_sync_at = row[0] or ""
            stored_schema = int(row[1] or 0)
    finally:
        conn.close()

    is_incremental = bool(last_sync_at) and not full_backfill
    if stored_schema < _SYNC_SCHEMA_VERSION:
        is_incremental = False
        _log.info(
            "HubSpot ticket sync schema upgrade (%s -> %s): full rediscovery required for engagement enrichment",
            stored_schema,
            _SYNC_SCHEMA_VERSION,
        )

    written = 0
    skipped = 0
    errors: list[str] = []
    budget_paused = False

    # Prepare list of properties to fetch
    default_props = [
        "subject",
        "content",
        "hs_pipeline_stage",
        "hs_ticket_category",
        "hs_ticket_priority",
        "createdate",
        "hs_lastmodifieddate",
        "resolution",
        "hs_resolution",
        "close_notes",
        "hs_close_notes",
    ]
    env_props = os.environ.get("HUBSPOT_TICKET_PROPERTIES", "").strip()
    if env_props:
        for p in env_props.split(","):
            p_clean = p.strip()
            if p_clean and p_clean not in default_props:
                default_props.append(p_clean)

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get closed stages
        closed_stages = await _get_closed_stage_ids(client, headers)
        _log.info("HubSpot Resolved Tickets Sync: monitoring stage IDs %s", closed_stages)

        tickets_data: list[dict[str, Any]] = []
        closed_candidates: list[dict[str, Any]] = []

        try:
            if is_incremental:
                # Use search API to pull recently modified tickets
                # Convert last_sync_at ISO string to millisecond timestamp or pass ISO string
                last_modified_filter = last_sync_at
                try:
                    # Parse ISO string to millisecond epoch timestamp if needed
                    dt = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))
                    # Subtract a small safety margin of 2 minutes to catch near-simultaneous modifications
                    last_modified_ms = int((dt.timestamp() - 120) * 1000)
                    last_modified_filter = str(last_modified_ms)
                except Exception:
                    pass

                _log.info("HubSpot Resolved Tickets Sync: Incremental sync since modified %s", last_modified_filter)

                offset = 0
                while True:
                    search_payload = {
                        "filterGroups": [
                            {
                                "filters": [
                                    {
                                        "propertyName": "hs_lastmodifieddate",
                                        "operator": "GT",
                                        "value": last_modified_filter,
                                    }
                                ]
                            }
                        ],
                        "limit": 100,
                        "properties": default_props,
                    }
                    if offset > 0:
                        # Paging search API requires 'after' parameter
                        search_payload["after"] = str(offset)

                    _consume_hubspot_budget(1)
                    resp = await client.post(
                        f"{_HUBSPOT_API}/crm/v3/objects/tickets/search",
                        headers=headers,
                        json=search_payload,
                    )

                    if resp.status_code == 401:
                        _set_progress(phase="error", running=False, error="HubSpot auth failed", message="Auth failed")
                        return {"ok": False, "error": "HubSpot auth failed — check HUBSPOT_PRIVATE_APP_TOKEN."}
                    elif resp.status_code == 403:
                        _set_progress(phase="error", running=False, error="Missing 'tickets' scopes", message="Scope error")
                        return {"ok": False, "error": "HubSpot token missing 'tickets' scopes to search."}
                    resp.raise_for_status()

                    payload = resp.json()
                    results = payload.get("results") or []
                    if not results:
                        break

                    tickets_data.extend(results)
                    _set_progress(discovered=len(tickets_data))
                    paging = payload.get("paging") or {}
                    next_page = paging.get("next") or {}
                    after_val = next_page.get("after")

                    if not after_val or len(results) < 100:
                        break
                    offset = after_val
                    await asyncio.sleep(0.25)  # Rate limiting safety
            else:
                # Full Backfill: use normal list GET API (no 10k search limit) and filter closed in python
                _log.info("HubSpot Resolved Tickets Sync: Full backfill of all tickets")
                next_after = None
                while True:
                    params: dict[str, Any] = {
                        "limit": 100,
                        "properties": ",".join(default_props),
                    }
                    if next_after:
                        params["after"] = next_after

                    _consume_hubspot_budget(1)
                    resp = await client.get(
                        f"{_HUBSPOT_API}/crm/v3/objects/tickets",
                        headers=headers,
                        params=params,
                    )

                    if resp.status_code == 401:
                        _set_progress(phase="error", running=False, error="HubSpot auth failed", message="Auth failed")
                        return {"ok": False, "error": "HubSpot auth failed — check HUBSPOT_PRIVATE_APP_TOKEN."}
                    elif resp.status_code == 403:
                        _set_progress(phase="error", running=False, error="Missing 'tickets' scopes", message="Scope error")
                        return {"ok": False, "error": "HubSpot token missing 'tickets' scopes to list."}
                    resp.raise_for_status()

                    payload = resp.json()
                    results = payload.get("results") or []
                    if not results:
                        break

                    tickets_data.extend(results)
                    _set_progress(discovered=len(tickets_data))
                    paging = payload.get("paging") or {}
                    next_page = paging.get("next") or {}
                    next_after = next_page.get("after")

                    if not next_after or len(results) < 100:
                        break
                    await asyncio.sleep(0.25)  # Rate limiting safety

        except HubSpotBudgetExceeded as exc:
            msg = "Daily HubSpot API budget reached during discovery — sync paused until tomorrow (UTC)."
            _set_progress(phase="idle", running=False, error="", message=msg)
            _log.warning("%s (%s)", msg, exc)
            return {"ok": False, "budget_paused": True, "error": msg}
        except Exception as exc:
            _log.exception("hubspot tickets download failed")
            _set_progress(phase="error", running=False, error=str(exc), message="Download failed")
            return {"ok": False, "error": str(exc)}

        _log.info("Downloaded %d candidate tickets. Processing resolved ones...", len(tickets_data))

        for ticket in tickets_data:
            ticket_id = str(ticket.get("id") or "").strip()
            if not ticket_id:
                continue
            properties = ticket.get("properties") or {}
            stage_id = str(properties.get("hs_pipeline_stage") or "").strip()
            if stage_id not in closed_stages:
                stage_label_lower = stage_id.lower()
                if not any(kw in stage_label_lower for kw in ("closed", "resolved", "done")):
                    skipped += 1
                    continue
            closed_candidates.append(ticket)

        _log.info(
            "HubSpot Resolved Tickets Sync: enriching %d closed tickets with emails, notes, calls, transcripts",
            len(closed_candidates),
        )
        _set_progress(
            phase="enriching",
            discovered=len(tickets_data),
            closed_candidates=len(closed_candidates),
            processed=0,
            message=f"Enriching {len(closed_candidates):,} resolved tickets…",
        )

        conn = sqlite3.connect(str(state_path))
        try:
            _init_state_db(conn)
            existing_hashes: dict[str, tuple[str, str]] = {}
            for row in conn.execute(
                "SELECT ticket_id, content_hash, engagements_hash FROM hubspot_tickets"
            ).fetchall():
                existing_hashes[str(row[0])] = (str(row[1] or ""), str(row[2] or ""))

            force_enrichment = full_backfill or stored_schema < _SYNC_SCHEMA_VERSION

            for batch_start in range(0, len(closed_candidates), _TICKET_PROCESS_BATCH):
                # Stop enriching once the daily HubSpot budget is spent; what we
                # already wrote still gets indexed below and the rest resumes on
                # the next (incremental) sync.
                if _hubspot_budget_remaining() <= 0:
                    budget_paused = True
                    break
                batch = closed_candidates[batch_start : batch_start + _TICKET_PROCESS_BATCH]
                batch_ids = [str(t.get("id") or "").strip() for t in batch if t.get("id")]
                try:
                    timelines = await fetch_ticket_timelines(
                        client,
                        headers,
                        batch_ids,
                        redact=_redact_pii,
                        include_conversations=True,
                    )
                except HubSpotBudgetExceeded:
                    budget_paused = True
                    break

                for ticket in batch:
                    try:
                        ticket_id = str(ticket.get("id") or "").strip()
                        if not ticket_id:
                            continue

                        properties = ticket.get("properties") or {}
                        stage_id = str(properties.get("hs_pipeline_stage") or "").strip()
                        subject = properties.get("subject") or f"Ticket #{ticket_id}"
                        content = properties.get("content") or ""

                        subject = _redact_pii(subject)
                        content = _redact_pii(content)

                        redacted_props: dict[str, Any] = {}
                        for k, v in properties.items():
                            if isinstance(v, str):
                                redacted_props[k] = _redact_pii(v)
                            else:
                                redacted_props[k] = v

                        created_at = properties.get("createdate") or ticket.get("createdAt") or ""
                        updated_at = properties.get("hs_lastmodifieddate") or ticket.get("updatedAt") or ""
                        category = properties.get("hs_ticket_category") or ""
                        priority = properties.get("hs_ticket_priority") or ""

                        slug = re.sub(r"[^\w\s-]+", "", subject.lower(), flags=re.UNICODE)
                        slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
                        slug_suffix = f"-{slug}" if slug else ""
                        dest = raw_dir / f"{ticket_id}{slug_suffix}.md"

                        timeline = timelines.get(ticket_id) or []
                        content_hash, engagements_hash = _write_ticket_markdown(
                            dest,
                            ticket_id=ticket_id,
                            subject=subject,
                            content=content,
                            created_at=created_at,
                            updated_at=updated_at,
                            stage_id=stage_id,
                            category=category,
                            priority=priority,
                            custom_props=redacted_props,
                            timeline=timeline,
                        )

                        prev = existing_hashes.get(ticket_id, ("", ""))
                        if (
                            not force_enrichment
                            and prev[0] == content_hash
                            and prev[1] == engagements_hash
                            and dest.is_file()
                        ):
                            continue

                        conn.execute(
                            """
                            INSERT INTO hubspot_tickets (
                              ticket_id, subject, stage_id, content_hash, engagements_hash, file_name, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(ticket_id) DO UPDATE SET
                              subject=excluded.subject,
                              stage_id=excluded.stage_id,
                              content_hash=excluded.content_hash,
                              engagements_hash=excluded.engagements_hash,
                              file_name=excluded.file_name,
                              updated_at=excluded.updated_at
                            """,
                            (
                                ticket_id,
                                subject,
                                stage_id,
                                content_hash,
                                engagements_hash,
                                dest.name,
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        written += 1

                    except Exception as exc:
                        _log.warning("ticket %s failed to sync: %s", ticket.get("id"), exc)
                        errors.append(f"{ticket.get('id')}: {exc}")

                conn.commit()
                processed_so_far = min(batch_start + _TICKET_PROCESS_BATCH, len(closed_candidates))
                _set_progress(
                    processed=processed_so_far,
                    written=written,
                    message=f"Enriched {processed_so_far:,} of {len(closed_candidates):,} resolved tickets…",
                )
                _log.info(
                    "Processed ticket batch %d-%d of %d (%d written so far)",
                    batch_start + 1,
                    min(batch_start + _TICKET_PROCESS_BATCH, len(closed_candidates)),
                    len(closed_candidates),
                    written,
                )
        finally:
            conn.close()

    # Rebuild KB search index
    if written > 0:
        _set_progress(phase="indexing", message="Rebuilding AI search index…")
        try:
            _rebuild_kb_index()
        except Exception as exc:
            _log.exception("kb rebuild failed after hubspot tickets sync")
            errors.append(f"index rebuild: {exc}")

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(state_path))
    try:
        _init_state_db(conn)
        conn.execute(
            """
            UPDATE hubspot_tickets_sync_state SET
              last_sync_at = ?,
              ticket_count = ?,
              last_error = ?,
              schema_version = ?
            WHERE id = 1
            """,
            (now, len(list(raw_dir.glob("*.md"))), "; ".join(errors[:3]), _SYNC_SCHEMA_VERSION),
        )
        conn.commit()
    finally:
        conn.close()

    done_message = (
        f"Paused at daily HubSpot budget — {written:,} tickets written so far; "
        f"remaining tickets resume on the next sync."
        if budget_paused
        else f"Done — {written:,} tickets written, {len(list(raw_dir.glob('*.md'))):,} on disk."
    )
    _set_progress(
        phase="done",
        running=False,
        processed=len(closed_candidates),
        written=written,
        message=done_message,
    )

    return {
        "ok": True,
        "budget_paused": budget_paused,
        "is_incremental": is_incremental,
        "tickets_discovered": len(tickets_data),
        "closed_tickets_processed": len(closed_candidates),
        "tickets_written": written,
        "tickets_skipped": skipped,
        "files_on_disk": len(list(raw_dir.glob("*.md"))),
        "enrichment": "full",
        "errors": errors[:10],
        "last_sync_at": now,
    }


def run_hubspot_tickets_sync(*, full_backfill: bool = False) -> dict[str, Any]:
    return asyncio.run(run_hubspot_tickets_sync_async(full_backfill=full_backfill))


_sync_running = False


async def start_hubspot_tickets_sync_background(*, full_backfill: bool = False) -> dict[str, Any]:
    global _sync_running
    if _sync_running:
        return {
            "ok": True,
            "started": False,
            "running": True,
            "message": "Ticket sync already running on persistent host",
            "status": hubspot_tickets_sync_status(),
        }

    async def _worker() -> None:
        global _sync_running
        try:
            await run_hubspot_tickets_sync_async(full_backfill=full_backfill)
        except Exception as exc:
            _log.exception("background hubspot tickets sync failed")
            _set_progress(phase="error", running=False, error=str(exc), message="Sync failed")
        finally:
            _sync_running = False
            _PROGRESS["running"] = False

    _sync_running = True
    asyncio.create_task(_worker())
    return {
        "ok": True,
        "started": True,
        "running": True,
        "message": "Ticket sync started on persistent host",
        "status": hubspot_tickets_sync_status(),
    }
