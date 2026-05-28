"""Knowledge admin helpers for Support Control dashboard."""

from __future__ import annotations

import hashlib
import html
import os
import re
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
    slack_docs = hubspot_docs = upload_docs = 0

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
            if "/slack/" in lower or lower.startswith("raw/slack"):
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

    slack_threads = int(slack_status.get("thread_count") or slack_docs)
    hubspot_articles = int(
        hubspot_status.get("files_on_disk")
        or hubspot_status.get("article_count")
        or hubspot_docs
    )

    wiki_dir = getattr(retriever, "wiki_dir", None)
    raw_dir = getattr(retriever, "support_raw_dir", None)
    db_path = getattr(retriever, "db_path", None)

    return {
        "total_chunks": total_chunks,
        "total_docs": len(docs),
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


def _classify_raw_source(doc_id: str) -> str:
    lower = doc_id.lower()
    if "hubspot-kb" in lower or "/hubspot/" in lower:
        return "HubSpot KB"
    if "/slack/" in lower:
        return "Slack"
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
        source_label = _classify_raw_source(doc_id)
        if source_label == "HubSpot KB":
            filename = Path(doc_id.split("/")[-1]).stem
            title = _markdown_h1(body) or _slug_to_title(filename)
            category = _extract_kv(full_text, "category")
            url = _extract_kv(full_text, "url")
            preview = _first_paragraph(body)
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


def list_knowledge_docs(retriever: Any) -> dict[str, Any]:
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

    source_order = {"Wiki": 0, "HubSpot KB": 1, "Playbook": 2, "Slack": 3, "Upload": 4}
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

    return {"docs": docs, "total": len(docs), "groups": groups, "serverless": _is_serverless()}


def get_doc_content(retriever: Any, doc_id: str) -> dict[str, Any]:
    chunks = [c for c in retriever._mem_chunks if c.doc_id == doc_id]
    if not chunks:
        return {"ok": False, "error": "Document not found"}
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


def search_knowledge(retriever: Any, query: str, k: int = 8) -> dict[str, Any]:
    q = query.strip()
    if not q:
        return {"results": [], "query": query}
    pairs = retriever.top_k(q, k=k)
    return {
        "query": q,
        "results": [
            {"doc_id": ch.doc_id, "chunk_id": ch.chunk_id, "score": round(sc, 4), "text": ch.text}
            for ch, sc in pairs
        ],
    }


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
