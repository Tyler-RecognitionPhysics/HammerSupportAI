"""Knowledge admin helpers for Support Control dashboard."""

from __future__ import annotations

import hashlib
import html
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _playbook_path(repo_root: Path) -> Path:
    pb_env = os.environ.get("SUPPORT_PLAYBOOK_MD", "").strip()
    if pb_env:
        return Path(pb_env).expanduser().resolve()
    return (repo_root / "knowledge_support" / "playbook" / "approved.md").resolve()


def _entry_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def _parse_playbook_entries(content: str) -> list[dict[str, Any]]:
    parts = re.split(r"(?m)^(###\s+.+)$", content)
    entries: list[dict[str, Any]] = []
    i = 1
    while i < len(parts):
        heading = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        full = (heading + "\n\n" + body).strip()
        entries.append({"id": _entry_hash(full), "heading": heading, "body": body})
        i += 2
    return entries


def knowledge_stats(retriever: Any, repo_root: Path | None = None) -> dict[str, Any]:
    docs_info = list_knowledge_docs(retriever)
    docs = docs_info["docs"]

    total_chunks = sum(d["chunk_count"] for d in docs)
    total_chars = sum(d["char_count"] for d in docs)

    wiki_docs = wiki_chunks = 0
    raw_docs = raw_chunks = 0
    playbook_chunks = 0
    slack_docs = hubspot_docs = hubspot_tickets_docs = upload_docs = 0

    for doc in docs:
        kind = doc["kind"]
        chunks = doc["chunk_count"]
        doc_id = doc["doc_id"]
        if kind == "wiki":
            wiki_docs += 1
            wiki_chunks += chunks
        elif kind == "playbook":
            playbook_chunks += chunks
        else:
            raw_docs += 1
            raw_chunks += chunks
            lower = doc_id.lower()
            if "hubspot-tickets" in lower:
                hubspot_tickets_docs += 1
            elif "/slack/" in lower or lower.startswith("raw/slack"):
                slack_docs += 1
            elif "hubspot-kb" in lower or "/hubspot/" in lower:
                hubspot_docs += 1
            else:
                upload_docs += 1

    playbook_entries = 0
    if repo_root is not None:
        playbook_entries = get_playbook(repo_root).get("entry_count", 0)

    slack_status: dict[str, Any] = {}
    try:
        from slack_sync import slack_sync_status

        slack_status = slack_sync_status()
    except Exception:
        slack_status = {}

    hubspot_status: dict[str, Any] = {}
    try:
        from hubspot_kb_sync import hubspot_kb_sync_status

        hubspot_status = hubspot_kb_sync_status()
    except Exception:
        hubspot_status = {}

    hubspot_tickets_status: dict[str, Any] = {}
    try:
        from hubspot_tickets_sync import hubspot_tickets_sync_status

        hubspot_tickets_status = hubspot_tickets_sync_status()
    except Exception:
        hubspot_tickets_status = {}

    slack_threads = int(slack_status.get("thread_count") or slack_docs)
    hubspot_articles = int(
        hubspot_status.get("files_on_disk")
        or hubspot_status.get("article_count")
        or hubspot_docs
    )
    hubspot_tickets_count = int(
        hubspot_tickets_status.get("indexed_tickets")
        or hubspot_tickets_status.get("files_on_disk")
        or hubspot_tickets_status.get("ticket_count")
        or hubspot_tickets_docs
    )

    wiki_dir = getattr(retriever, "wiki_dir", None)
    raw_dir = getattr(retriever, "support_raw_dir", None)
    db_path = getattr(retriever, "db_path", None)

    sqlite_docs = sqlite_chunks = sqlite_ticket_docs = sqlite_email_ticket_docs = 0
    if db_path and Path(db_path).is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                sqlite_docs = int(conn.execute("SELECT COUNT(*) FROM kb_document").fetchone()[0])
                sqlite_chunks = int(conn.execute("SELECT COUNT(*) FROM kb_chunk").fetchone()[0])
                sqlite_ticket_docs = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM kb_document WHERE path LIKE '%hubspot-tickets%'"
                    ).fetchone()[0]
                )
                # Tickets worked via email — their cached timeline contains at least one
                # rendered email activity ("### … — Email: …"). These tend to carry the
                # richest step-by-step resolutions. Computed from already-cached data only.
                sqlite_email_ticket_docs = int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT c.document_id)
                        FROM kb_chunk c
                        JOIN kb_document d ON d.id = c.document_id
                        WHERE d.path LIKE '%hubspot-tickets%'
                          AND c.text LIKE ?
                        """,
                        ("%\u2014 Email:%",),
                    ).fetchone()[0]
                )
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    if sqlite_ticket_docs > hubspot_tickets_count:
        hubspot_tickets_count = sqlite_ticket_docs
    if sqlite_ticket_docs > hubspot_tickets_docs:
        hubspot_tickets_docs = sqlite_ticket_docs
    if sqlite_docs > len(docs):
        total_docs = sqlite_docs
        total_chunks = max(total_chunks, sqlite_chunks)
    else:
        total_docs = len(docs)

    return {
        "total_chunks": max(total_chunks, sqlite_chunks),
        "total_docs": total_docs,
        "total_chars": total_chars,
        "wiki_docs": wiki_docs,
        "wiki_chunks": wiki_chunks,
        "raw_docs": raw_docs,
        "raw_chunks": raw_chunks,
        "slack_docs": slack_docs,
        "slack_threads": slack_threads,
        "slack_configured": bool(slack_status.get("configured")),
        "hubspot_docs": hubspot_docs,
        "hubspot_articles": hubspot_articles,
        "hubspot_configured": bool(hubspot_status.get("configured")),
        "hubspot_tickets_docs": hubspot_tickets_docs,
        "hubspot_tickets_count": hubspot_tickets_count,
        "hubspot_email_tickets_count": sqlite_email_ticket_docs,
        "hubspot_tickets_configured": bool(hubspot_tickets_status.get("configured")),
        "upload_docs": upload_docs,
        "playbook_entries": playbook_entries,
        "playbook_chunks": playbook_chunks,
        "unique_terms": len(getattr(retriever, "_idf", {})),
        "sqlite_ok": bool(getattr(retriever, "_sqlite_ok", False)),
        "wiki_dir": str(wiki_dir) if wiki_dir else "",
        "raw_dir": str(raw_dir) if raw_dir else "",
        "db_path": str(db_path) if db_path else "",
    }


def _slug_to_title(slug: str) -> str:
    slug = re.sub(r"^\d+-", "", slug)
    slug = slug.replace("_", "-")
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip()
    return meta, body


def _extract_kv(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.I | re.M)
    return match.group(1).strip() if match else ""


def _markdown_h1(text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return match.group(1).strip() if match else ""


def _section_body(text: str, name: str) -> str:
    """Return the text under a `## {name}` heading, up to the next `##`/`#`."""
    match = re.search(
        rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^#{{1,2}}\s|\Z)",
        text,
        re.I | re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def _strip_markdown_noise(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _slack_thread_date(body: str) -> str:
    match = re.search(r"\((\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s+UTC\)", body)
    return match.group(1) if match else ""


def _first_slack_message(body: str) -> str:
    lines = body.splitlines()
    reading = False
    for line in lines:
        if re.match(r"^\*\*.+\*\* \(\d{4}-\d{2}-\d{2}", line):
            reading = True
            continue
        if not reading or not line.strip():
            continue
        cleaned = _strip_markdown_noise(line)
        if len(cleaned) < 8:
            continue
        if cleaned in {">", "&gt;"}:
            continue
        if cleaned.startswith("@"):
            continue
        return cleaned
    return ""


def _first_paragraph(body: str, *, skip_lines: int = 0) -> str:
    lines = body.splitlines()[skip_lines:]
    parts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if parts:
                break
            continue
        if stripped.startswith("#"):
            continue
        if re.match(r"^[a-z_]+:\s", stripped, re.I):
            continue
        if stripped.startswith("---"):
            continue
        parts.append(_strip_markdown_noise(stripped))
        if len(" ".join(parts)) >= 180:
            break
    return " ".join(parts)


def _truncate(text: str, limit: int = 180) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _classify_raw_source(doc_id: str, *, meta: dict[str, str] | None = None) -> str:
    lower = doc_id.lower()
    meta_source = (meta or {}).get("source", "").strip().lower()
    if meta_source in ("hubspot-tickets", "hubspot_tickets"):
        return "HubSpot Resolved Tickets"
    if "hubspot-tickets" in lower or "hubspot_tickets" in lower:
        return "HubSpot Resolved Tickets"
    if re.search(r"[-/]\d+-ticket(?:-|\.)", lower) or lower.endswith("-ticket.md"):
        return "HubSpot Resolved Tickets"
    if "hubspot-kb" in lower or "/hubspot/" in lower:
        return "HubSpot KB"
    if "/slack/" in lower:
        return "Slack"
    if "dashboard-uploads" in lower:
        return "Upload"
    return "Upload"


def _doc_display_fields(doc_id: str, kind: str, full_text: str) -> dict[str, str]:
    meta, body = _split_frontmatter(full_text)
    title = ""
    preview = ""
    category = ""
    url = ""
    date = meta.get("date", "")
    source_label = "Wiki"

    if kind == "playbook":
        source_label = "Playbook"
        title = "Approved support answers"
        heading = re.search(r"^###\s+(.+)$", body, re.M)
        preview = heading.group(1).strip() if heading else _first_paragraph(body)
    elif kind == "wiki":
        source_label = "Wiki"
        stem = Path(doc_id).stem
        title = _slug_to_title(stem)
        if doc_id.startswith("topics/"):
            category = "Topic Q&A"
        h1 = _markdown_h1(body)
        if h1:
            title = h1
        preview = _first_paragraph(body)
    else:
        source_label = _classify_raw_source(doc_id, meta=meta)
        if source_label == "HubSpot KB":
            filename = Path(doc_id.split("/")[-1]).stem
            title = _markdown_h1(body) or _slug_to_title(filename)
            category = _extract_kv(full_text, "category")
            url = _extract_kv(full_text, "url")
            preview = _first_paragraph(body)
        elif source_label == "HubSpot Resolved Tickets":
            subject = _markdown_h1(body) or _slug_to_title(Path(doc_id.split("/")[-1]).stem)
            desc = _section_body(body, "Description")
            issue = _first_paragraph(desc) if desc else ""
            if issue and len(issue) >= 10 and issue.lower() != subject.lower():
                # Title by what the ticket is actually about; keep the dealership as context.
                title = _truncate(issue, 90)
                preview = subject
            else:
                title = subject
                preview = _first_paragraph(body, skip_lines=0)
            category = meta.get("category") or _extract_kv(full_text, "category")
            date = meta.get("updated_at") or meta.get("created_at") or date
        elif source_label == "Slack":
            date = meta.get("date") or _slack_thread_date(body)
            preview = _first_slack_message(body)
            if preview:
                title = _truncate(preview, 96)
            else:
                title = f"Support thread · {date or 'Slack'}"
            category = "Support channel"
        else:
            title = _slug_to_title(Path(doc_id.split("/")[-1]).stem)
            preview = _first_paragraph(body)

    if not preview:
        preview = _first_paragraph(body)
    preview = _truncate(preview)

    return {
        "title": title or doc_id,
        "source_label": source_label,
        "preview": preview,
        "category": category,
        "url": url,
        "date": date,
    }


def _enrich_doc_record(doc_id: str, kind: str, chunk_count: int, char_count: int, full_text: str) -> dict[str, Any]:
    display = _doc_display_fields(doc_id, kind, full_text)
    return {
        "doc_id": doc_id,
        "kind": kind,
        "chunk_count": chunk_count,
        "char_count": char_count,
        **display,
    }


def _source_label_from_path(path: str, kind: str, *, meta: dict[str, str] | None = None) -> str:
    if path.startswith("playbook/"):
        return "Playbook"
    if kind == "wiki" and not path.startswith("raw/"):
        return "Wiki"
    if path.startswith("raw/") or kind == "raw":
        return _classify_raw_source(path, meta=meta)
    return "Wiki"


def _kind_from_path(path: str, db_kind: str) -> str:
    if path.startswith("playbook/"):
        return "playbook"
    if path.startswith("raw/"):
        return "raw"
    if db_kind == "wiki":
        return "wiki"
    return db_kind or "raw"


def _sqlite_group_totals(db_path: Path) -> dict[str, int]:
    totals: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                """
                SELECT
                  CASE
                    WHEN path LIKE '%hubspot-tickets%' THEN 'HubSpot Resolved Tickets'
                    WHEN path LIKE '%hubspot-kb%' OR path LIKE '%/hubspot/%' THEN 'HubSpot KB'
                    WHEN path LIKE '%/slack/%' THEN 'Slack'
                    WHEN path LIKE 'playbook/%' THEN 'Playbook'
                    WHEN kind = 'wiki' AND path NOT LIKE 'raw/%' THEN 'Wiki'
                    WHEN path LIKE '%dashboard-uploads%' THEN 'Upload'
                    ELSE 'Upload'
                  END AS label,
                  COUNT(*) AS n
                FROM kb_document
                GROUP BY label
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return totals
    for label, count in rows:
        totals[str(label)] = int(count)
    legacy = totals.pop("HubSpot Tickets", 0)
    if legacy:
        totals["HubSpot Resolved Tickets"] = totals.get("HubSpot Resolved Tickets", 0) + legacy
    return totals


def _sqlite_fetch_documents(
    db_path: Path,
    *,
    ticket_offset: int = 0,
    ticket_limit: int = 200,
    tickets_only: bool = False,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not tickets_only:
                rows = conn.execute(
                    """
                    SELECT d.path, d.title, d.kind, d.body,
                           (SELECT COUNT(*) FROM kb_chunk c WHERE c.document_id = d.id) AS chunk_count,
                           (SELECT COALESCE(SUM(LENGTH(c.text)), 0) FROM kb_chunk c WHERE c.document_id = d.id) AS char_count
                    FROM kb_document d
                    WHERE d.path NOT LIKE '%hubspot-tickets%'
                    ORDER BY d.path
                    """
                ).fetchall()
                for row in rows:
                    path = str(row["path"])
                    kind = _kind_from_path(path, str(row["kind"]))
                    docs.append(
                        _enrich_doc_record(
                            path,
                            kind,
                            int(row["chunk_count"] or 0),
                            int(row["char_count"] or 0),
                            str(row["body"] or ""),
                        )
                    )

            if ticket_limit > 0:
                ticket_rows = conn.execute(
                    """
                    SELECT d.path, d.title, d.kind, SUBSTR(d.body, 1, 4000) AS body,
                           (SELECT COUNT(*) FROM kb_chunk c WHERE c.document_id = d.id) AS chunk_count,
                           (SELECT COALESCE(SUM(LENGTH(c.text)), 0) FROM kb_chunk c WHERE c.document_id = d.id) AS char_count
                    FROM kb_document d
                    WHERE d.path LIKE '%hubspot-tickets%'
                    ORDER BY d.path DESC
                    LIMIT ? OFFSET ?
                    """,
                    (ticket_limit, ticket_offset),
                ).fetchall()
                for row in ticket_rows:
                    path = str(row["path"])
                    kind = _kind_from_path(path, str(row["kind"]))
                    docs.append(
                        _enrich_doc_record(
                            path,
                            kind,
                            int(row["chunk_count"] or 0),
                            int(row["char_count"] or 0),
                            str(row["body"] or ""),
                        )
                    )
        finally:
            conn.close()
    except sqlite3.Error:
        return docs
    return docs


def list_knowledge_docs(
    retriever: Any,
    *,
    ticket_offset: int = 0,
    ticket_limit: int = 200,
    tickets_only: bool = False,
) -> dict[str, Any]:
    db_path = getattr(retriever, "db_path", None)
    if getattr(retriever, "_sqlite_ok", False) and db_path and Path(db_path).is_file():
        path = Path(db_path)
        group_totals = _sqlite_group_totals(path)
        docs = _sqlite_fetch_documents(
            path,
            ticket_offset=max(0, ticket_offset),
            ticket_limit=max(0, min(ticket_limit, 500)),
            tickets_only=tickets_only,
        )
        ticket_total = group_totals.get("HubSpot Resolved Tickets", 0)
        ticket_loaded = sum(1 for d in docs if "hubspot-tickets" in d.get("doc_id", "").lower())
        from kb_source_control import knowledge_sources_state

        source_state = knowledge_sources_state(group_totals=group_totals)
        return {
            "docs": docs,
            "total": sum(group_totals.values()),
            "groups": group_totals,
            "group_totals": group_totals,
            "ticket_total": ticket_total,
            "ticket_offset": ticket_offset,
            "ticket_limit": ticket_limit,
            "ticket_loaded": ticket_loaded,
            "serverless": _is_serverless(),
            "enabled_sources": source_state["enabled"],
            "source_types": source_state["sources"],
        }

    grouped: dict[str, list[Any]] = {}
    counts: dict[str, dict[str, int]] = {}

    for chunk in retriever._mem_chunks:
        doc_id: str = chunk.doc_id
        grouped.setdefault(doc_id, []).append(chunk)
        if doc_id not in counts:
            if doc_id.startswith("playbook/"):
                kind = "playbook"
            elif doc_id.startswith("raw/"):
                kind = "raw"
            else:
                kind = "wiki"
            counts[doc_id] = {"kind": kind, "chunk_count": 0, "char_count": 0}
        counts[doc_id]["chunk_count"] += 1
        counts[doc_id]["char_count"] += len(chunk.text)

    docs: list[dict[str, Any]] = []
    for doc_id, meta in counts.items():
        chunks = sorted(grouped[doc_id], key=lambda c: c.chunk_id)
        full_text = "\n\n".join(c.text for c in chunks)
        docs.append(_enrich_doc_record(doc_id, meta["kind"], meta["chunk_count"], meta["char_count"], full_text))

    source_order = {
        "Wiki": 0,
        "HubSpot KB": 1,
        "Playbook": 2,
        "Slack": 3,
        "HubSpot Resolved Tickets": 4,
        "HubSpot Tickets": 4,
        "Upload": 5,
    }
    docs.sort(
        key=lambda d: (
            source_order.get(d["source_label"], 9),
            d.get("date") or "",
            (d.get("title") or d["doc_id"]).lower(),
        )
    )

    groups: dict[str, int] = {}
    for doc in docs:
        label = doc["source_label"]
        groups[label] = groups.get(label, 0) + 1

    from kb_source_control import knowledge_sources_state

    source_state = knowledge_sources_state(group_totals=groups)
    return {
        "docs": docs,
        "total": len(docs),
        "groups": groups,
        "group_totals": groups,
        "serverless": _is_serverless(),
        "enabled_sources": source_state["enabled"],
        "source_types": source_state["sources"],
    }


# Tickets worked via email render at least one "### … — Email: …" timeline entry.
_EMAIL_TICKET_MARKER_LIKE = "%\u2014 Email:%"


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards so user search text is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_email_worked_tickets(
    retriever: Any,
    *,
    offset: int = 0,
    limit: int = 100,
    q: str = "",
) -> dict[str, Any]:
    """List HubSpot tickets that were worked via email (from already-cached data only).

    Email-worked tickets carry the richest step-by-step resolutions, so this lets the
    operator browse and inspect every one without any additional HubSpot API calls.
    When ``q`` is provided, only tickets whose content contains that text are returned.
    """
    db_path = getattr(retriever, "db_path", None)
    offset = max(0, offset)
    limit = max(1, min(limit, 300))
    q = (q or "").strip()
    search_clause = ""
    search_params: list[Any] = []
    if q:
        search_clause = " AND d.body LIKE ? ESCAPE '\\'"
        search_params.append(f"%{_like_escape(q)}%")
    if not (getattr(retriever, "_sqlite_ok", False) and db_path and Path(db_path).is_file()):
        return {
            "ok": False,
            "docs": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "error": "Knowledge index not available.",
        }

    docs: list[dict[str, Any]] = []
    total = 0
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM kb_document d
                    WHERE d.path LIKE '%hubspot-tickets%'
                      AND d.id IN (
                        SELECT DISTINCT c.document_id FROM kb_chunk c WHERE c.text LIKE ?
                      ){search_clause}
                    """,
                    (_EMAIL_TICKET_MARKER_LIKE, *search_params),
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT d.path, d.title, d.kind, SUBSTR(d.body, 1, 4000) AS body,
                       (SELECT COUNT(*) FROM kb_chunk c WHERE c.document_id = d.id) AS chunk_count,
                       (SELECT COALESCE(SUM(LENGTH(c.text)), 0) FROM kb_chunk c WHERE c.document_id = d.id) AS char_count
                FROM kb_document d
                WHERE d.path LIKE '%hubspot-tickets%'
                  AND d.id IN (
                    SELECT DISTINCT c.document_id FROM kb_chunk c WHERE c.text LIKE ?
                  ){search_clause}
                ORDER BY d.path DESC
                LIMIT ? OFFSET ?
                """,
                (_EMAIL_TICKET_MARKER_LIKE, *search_params, limit, offset),
            ).fetchall()
            for row in rows:
                path = str(row["path"])
                kind = _kind_from_path(path, str(row["kind"]))
                record = _enrich_doc_record(
                    path,
                    kind,
                    int(row["chunk_count"] or 0),
                    int(row["char_count"] or 0),
                    str(row["body"] or ""),
                )
                ticket_id = _ticket_id_from_doc_id(path)
                record["ticket_id"] = ticket_id
                if not record.get("url"):
                    record["url"] = _hubspot_ticket_url(ticket_id)
                docs.append(record)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "docs": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "error": str(exc),
        }

    return {
        "ok": True,
        "docs": docs,
        "total": total,
        "offset": offset,
        "limit": limit,
        "loaded": len(docs),
    }


def get_doc_content(retriever: Any, doc_id: str) -> dict[str, Any]:
    chunks = [c for c in retriever._mem_chunks if c.doc_id == doc_id]
    if chunks:
        chunks = sorted(chunks, key=lambda c: c.chunk_id)
        full_text = "\n\n".join(c.text for c in chunks)
        if doc_id.startswith("playbook/"):
            kind = "playbook"
        elif doc_id.startswith("raw/"):
            kind = "raw"
        else:
            kind = "wiki"
        display = _doc_display_fields(doc_id, kind, full_text)
        return {
            "ok": True,
            "doc_id": doc_id,
            "text": full_text,
            "title": display["title"],
            "source_label": display["source_label"],
            "preview": display["preview"],
            "category": display["category"],
            "url": display["url"],
            "date": display["date"],
            "chunk_count": len(chunks),
        }

    db_path = getattr(retriever, "db_path", None)
    if getattr(retriever, "_sqlite_ok", False) and db_path and Path(db_path).is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT path, title, body, kind FROM kb_document WHERE path = ?",
                    (doc_id,),
                ).fetchone()
                if row:
                    path = str(row[0])
                    kind = _kind_from_path(path, str(row[3]))
                    full_text = str(row[2] or "")
                    display = _doc_display_fields(path, kind, full_text)
                    chunk_count = conn.execute(
                        """
                        SELECT COUNT(*) FROM kb_chunk
                        WHERE document_id = (SELECT id FROM kb_document WHERE path = ?)
                        """,
                        (doc_id,),
                    ).fetchone()[0]
                    return {
                        "ok": True,
                        "doc_id": doc_id,
                        "text": full_text,
                        "title": display["title"],
                        "source_label": display["source_label"],
                        "preview": display["preview"],
                        "category": display["category"],
                        "url": display["url"],
                        "date": display["date"],
                        "chunk_count": int(chunk_count),
                    }
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    return {"ok": False, "error": "Document not found"}


def _annotate_relevance_scores(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn internal RRF scores (~0.01–0.08) into user-facing match labels."""
    if not items:
        return items
    scores = [float(x.get("score") or 0) for x in items]
    max_sc = max(scores) if scores else 0.0
    if max_sc <= 0:
        return [
            {
                **item,
                "relevance_pct": 0,
                "relevance_label": "Related",
                "relevance_tier": "related",
            }
            for item in items
        ]

    out: list[dict[str, Any]] = []
    for item in items:
        sc = float(item.get("score") or 0)
        ratio = sc / max_sc
        pct = int(round(ratio * 100))
        is_top = abs(sc - max_sc) < 1e-9
        if is_top:
            label, tier = "Best match", "best"
        elif ratio >= 0.75:
            label, tier = "Strong match", "strong"
        elif ratio >= 0.55:
            label, tier = "Good match", "good"
        elif ratio >= 0.35:
            label, tier = "Moderate match", "moderate"
        else:
            label, tier = "Related", "related"
        out.append(
            {
                **item,
                "relevance_pct": pct,
                "relevance_label": label,
                "relevance_tier": tier,
            }
        )
    return out


def search_knowledge(retriever: Any, query: str, k: int = 8) -> dict[str, Any]:
    q = query.strip()
    if not q:
        return {"results": [], "query": query}
    pairs = retriever.top_k(q, k=k)
    results = [
        {"doc_id": ch.doc_id, "chunk_id": ch.chunk_id, "score": round(sc, 4), "text": ch.text}
        for ch, sc in pairs
    ]
    return {
        "query": q,
        "results": _annotate_relevance_scores(results),
    }


def _ticket_id_from_doc_id(doc_id: str) -> str:
    stem = Path(str(doc_id or "").split("/")[-1]).stem
    first = stem.split("-", 1)[0].strip()
    return first if first.isdigit() else ""


def _hubspot_ticket_url(ticket_id: str) -> str:
    portal = os.environ.get("HUBSPOT_PORTAL_ID", "3355079").strip()
    tid = str(ticket_id or "").strip()
    if portal and tid:
        return f"https://app.hubspot.com/contacts/{portal}/ticket/{tid}"
    return ""


def _enrich_source_record(retriever: Any, source: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(source.get("doc_id") or "")
    enriched = dict(source)
    doc = get_doc_content(retriever, doc_id)
    if doc.get("ok"):
        enriched["title"] = doc.get("title") or doc_id
        enriched["source_label"] = doc.get("source_label") or ""
        enriched["category"] = doc.get("category") or ""
        enriched["preview"] = doc.get("preview") or ""
        enriched["url"] = doc.get("url") or ""
    else:
        enriched["title"] = _slug_to_title(Path(doc_id.split("/")[-1]).stem)
        enriched["source_label"] = _classify_raw_source(doc_id)
    if enriched.get("source_group") == "helpdesk_ticket" or enriched.get("source_label") == "HubSpot Resolved Tickets":
        ticket_id = _ticket_id_from_doc_id(doc_id)
        enriched["ticket_id"] = ticket_id
        enriched["ticket_url"] = _hubspot_ticket_url(ticket_id)
        if not enriched.get("url"):
            enriched["url"] = enriched["ticket_url"]
    return enriched


def _normalize_match_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*#`_>\[\]|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _meaningful_words(text: str) -> list[str]:
    stop = {
        "that", "this", "with", "from", "have", "your", "will", "they", "them",
        "then", "when", "what", "where", "which", "about", "into", "also", "just",
        "please", "follow", "these", "steps", "open", "link", "browser",
    }
    return [w for w in re.findall(r"[a-z0-9]{4,}", _normalize_match_text(text)) if w not in stop]


def _find_used_excerpts(source_text: str, response: str) -> list[str]:
    response_norm = _normalize_match_text(response)
    if not response_norm:
        return []

    excerpts: list[str] = []
    seen: set[str] = set()

    def consider(part: str) -> None:
        chunk = part.strip()
        if len(chunk) < 10:
            return
        chunk_norm = _normalize_match_text(chunk)
        if not chunk_norm or chunk_norm in seen:
            return
        if chunk_norm in response_norm:
            seen.add(chunk_norm)
            excerpts.append(chunk)
            return
        words = _meaningful_words(chunk)
        if len(words) >= 2:
            hits = sum(1 for w in words if w in response_norm)
            if hits >= max(2, int(len(words) * 0.45)):
                seen.add(chunk_norm)
                excerpts.append(chunk)

    for part in re.split(r"(?<=[.!?])\s+|\n+", source_text):
        consider(part)

    if not excerpts:
        for line in source_text.splitlines():
            consider(line.strip().lstrip("-*># ").strip())

    return excerpts[:6]


def _highlight_excerpts_in_text(full_text: str, excerpts: list[str]) -> str:
    out = html.escape(full_text)
    for excerpt in sorted(excerpts, key=len, reverse=True):
        escaped = html.escape(excerpt)
        if escaped in out:
            out = out.replace(
                escaped,
                f'<mark class="kb-used">{escaped}</mark>',
                1,
            )
    return out


def _match_strength(excerpts: list[str], response: str) -> str:
    if not excerpts:
        return "context_only"
    response_norm = _normalize_match_text(response)
    for excerpt in excerpts:
        if _normalize_match_text(excerpt) in response_norm:
            return "direct"
    return "partial"


def _analyze_source_usage(source: dict[str, Any], response: str, citation_index: int) -> dict[str, Any]:
    text = str(source.get("text") or "")
    excerpts = _find_used_excerpts(text, response)
    strength = _match_strength(excerpts, response)
    return {
        **source,
        "citation_index": citation_index,
        "used_excerpts": excerpts,
        "used_excerpt_html": _highlight_excerpts_in_text(text, excerpts) if excerpts else "",
        "match_strength": strength,
    }


def _sort_sources_for_display(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {"direct": 0, "partial": 1, "context_only": 2}
    return sorted(
        sources,
        key=lambda s: (
            order.get(str(s.get("match_strength") or "context_only"), 9),
            -float(s.get("score") or 0),
        ),
    )


async def ask_knowledge(
    retriever: Any,
    executor: Any,
    question: str,
) -> dict[str, Any]:
    from support_chat import preview_support_response

    result = await preview_support_response(executor, retriever, question)
    if not result.get("ok"):
        return result

    response_text = str(result.get("response") or "")
    sources: list[dict[str, Any]] = []
    for i, raw in enumerate(result.get("sources") or [], start=1):
        enriched = _enrich_source_record(retriever, raw)
        sources.append(_analyze_source_usage(enriched, response_text, i))
    annotated = _sort_sources_for_display(_annotate_relevance_scores(sources))
    result["sources"] = annotated
    tickets: dict[str, dict[str, Any]] = {}
    for source in annotated:
        if source.get("source_group") != "helpdesk_ticket":
            continue
        key = str(source.get("case_doc_id") or source.get("doc_id") or "")
        if not key:
            continue
        bucket = tickets.setdefault(
            key,
            {
                "doc_id": key,
                "case_rank": source.get("case_rank") or 0,
                "case_score": source.get("case_score") or 0,
                "solution_score": source.get("solution_score") or 0,
                "email_worked": bool(source.get("email_worked")),
                "ticket_id": source.get("ticket_id") or _ticket_id_from_doc_id(key),
                "ticket_url": source.get("ticket_url") or _hubspot_ticket_url(_ticket_id_from_doc_id(key)),
                "title": source.get("title") or key,
                "pinned": bool(source.get("pinned")),
                "source_count": 0,
                "used_excerpt_count": 0,
                "citations": [],
            },
        )
        if source.get("pinned"):
            bucket["pinned"] = True
        bucket["source_count"] += 1
        bucket["used_excerpt_count"] += len(source.get("used_excerpts") or [])
        bucket["citations"].append(source.get("citation_index"))
    result["helpdesk_cases"] = sorted(
        tickets.values(),
        key=lambda x: (int(x.get("case_rank") or 999), -float(x.get("case_score") or 0)),
    )
    return result


async def regenerate_knowledge(
    retriever: Any,
    question: str,
    correct_info: str,
) -> dict[str, Any]:
    """Regenerate Hannah's answer from admin-provided correct info, with source cards."""
    from support_chat import regenerate_support_response

    result = await regenerate_support_response(retriever, question, correct_info)
    if not result.get("ok"):
        return result

    response_text = str(result.get("response") or "")
    sources: list[dict[str, Any]] = []
    for i, raw in enumerate(result.get("sources") or [], start=1):
        enriched = _enrich_source_record(retriever, raw)
        sources.append(_analyze_source_usage(enriched, response_text, i))
    result["sources"] = _sort_sources_for_display(_annotate_relevance_scores(sources))
    return result


def list_ticket_pins(repo_root: Path) -> dict[str, Any]:
    from support_ticket_pins import list_pins

    return list_pins(repo_root)


def add_ticket_pin(retriever: Any, repo_root: Path, topic: str, doc_id: str) -> dict[str, Any]:
    from support_ticket_pins import add_pin

    doc_id = str(doc_id or "").strip()
    topic = str(topic or "").strip()
    if not doc_id:
        return {"ok": False, "error": "A ticket is required."}
    if not topic:
        return {"ok": False, "error": "A question/topic is required."}

    ticket_id = _ticket_id_from_doc_id(doc_id)
    ticket_url = _hubspot_ticket_url(ticket_id)
    title = ""
    try:
        doc = get_doc_content(retriever, doc_id)
        if doc.get("ok"):
            title = str(doc.get("title") or "")
    except Exception:
        title = ""

    return add_pin(
        repo_root,
        topic=topic,
        ticket_doc_id=doc_id,
        ticket_id=ticket_id,
        ticket_url=ticket_url,
        title=title,
    )


def delete_ticket_pin(repo_root: Path, pin_id: str) -> dict[str, Any]:
    from support_ticket_pins import delete_pin

    return delete_pin(repo_root, pin_id)


def get_playbook(repo_root: Path) -> dict[str, Any]:
    pb_path = _playbook_path(repo_root)
    content = pb_path.read_text(encoding="utf-8") if pb_path.is_file() else ""
    entries = _parse_playbook_entries(content)
    return {"entries": entries, "entry_count": len(entries), "editable": not _is_serverless()}


def append_playbook_entry(repo_root: Path, title: str, content: str) -> dict[str, Any]:
    if _is_serverless():
        return {"ok": False, "error": "Playbook edits are read-only on production."}
    if not title.strip() or not content.strip():
        return {"ok": False, "error": "Title and content required."}
    pb_path = _playbook_path(repo_root)
    pb_path.parent.mkdir(parents=True, exist_ok=True)
    heading = title.strip() if title.strip().startswith("###") else f"### {title.strip()}"
    block = f"{heading}\n\n{content.strip()}"
    prev = pb_path.read_text(encoding="utf-8") if pb_path.is_file() else ""
    pb_path.write_text(prev.rstrip() + ("\n\n" if prev.strip() else "") + block + "\n", encoding="utf-8")
    return {"ok": True, "id": _entry_hash(block)}


def delete_playbook_entry(repo_root: Path, entry_id: str) -> dict[str, Any]:
    if _is_serverless():
        return {"ok": False, "error": "Playbook edits are read-only on production."}
    pb_path = _playbook_path(repo_root)
    if not pb_path.is_file():
        return {"ok": False, "error": "Playbook not found."}
    content = pb_path.read_text(encoding="utf-8")
    entries = _parse_playbook_entries(content)
    target = next((e for e in entries if e["id"] == entry_id), None)
    if not target:
        return {"ok": False, "error": "Entry not found."}
    new_content = content.replace(target["heading"] + "\n\n" + target["body"], "").strip()
    if new_content:
        new_content += "\n"
    pb_path.write_text(new_content, encoding="utf-8")
    return {"ok": True}
