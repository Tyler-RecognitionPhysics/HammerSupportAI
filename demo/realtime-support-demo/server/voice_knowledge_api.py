"""Knowledge base admin helpers for the voice admin dashboard.

Exposes helpers for listing indexed documents, running retrieval test searches,
and reading / editing the playbook (knowledge/playbook/approved.md).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_serverless() -> bool:
    return os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in (
        "1", "true", "yes"
    )


def _playbook_path(repo_root: Path) -> Path:
    pb_env = os.environ.get("REALTIME_SALES_PLAYBOOK_MD", "").strip()
    if pb_env:
        return Path(pb_env).expanduser().resolve()
    return (repo_root / "knowledge" / "playbook" / "approved.md").resolve()


def _entry_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def _parse_playbook_entries(content: str) -> list[dict[str, Any]]:
    """Split playbook markdown into sections by `###` headings."""
    # Split on lines that start with ###
    parts = re.split(r"(?m)^(###\s+.+)$", content)
    entries: list[dict[str, Any]] = []
    # parts layout: [preamble, heading1, body1, heading2, body2, …]
    i = 1
    while i < len(parts):
        heading = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        full = (heading + "\n\n" + body).strip()
        entries.append(
            {
                "id": _entry_hash(full),
                "heading": heading,
                "body": body,
            }
        )
        i += 2
    return entries


# ---------------------------------------------------------------------------
# Public API helpers (called from app.py route handlers)
# ---------------------------------------------------------------------------

def list_knowledge_docs(retriever: Any) -> dict[str, Any]:
    """Return a deduplicated list of all indexed document metadata."""
    seen: dict[str, dict[str, Any]] = {}
    for chunk in retriever._mem_chunks:
        doc_id: str = chunk.doc_id
        if doc_id not in seen:
            if doc_id.startswith("playbook/"):
                kind = "playbook"
            elif doc_id.startswith("raw/"):
                kind = "raw"
            else:
                kind = "wiki"
            seen[doc_id] = {
                "doc_id": doc_id,
                "kind": kind,
                "chunk_count": 0,
                "char_count": 0,
            }
        seen[doc_id]["chunk_count"] += 1
        seen[doc_id]["char_count"] += len(chunk.text)

    docs = list(seen.values())
    order = {"wiki": 0, "playbook": 1, "raw": 2}
    docs.sort(key=lambda d: (order.get(d["kind"], 9), d["doc_id"]))
    return {
        "docs": docs,
        "total": len(docs),
        "serverless": _is_serverless(),
    }


def get_doc_content(retriever: Any, doc_id: str) -> dict[str, Any]:
    """Return the full text of one indexed document (chunks concatenated)."""
    chunks = [c for c in retriever._mem_chunks if c.doc_id == doc_id]
    if not chunks:
        return {"ok": False, "error": "Document not found"}
    chunks = sorted(chunks, key=lambda c: c.chunk_id)
    return {
        "ok": True,
        "doc_id": doc_id,
        "text": "\n\n".join(c.text for c in chunks),
    }


def search_knowledge(retriever: Any, query: str, k: int = 8) -> dict[str, Any]:
    """Test BM25 retrieval — identical path to the live AI search_wiki tool."""
    q = query.strip()
    if not q:
        return {"results": [], "query": query}
    pairs = retriever.top_k(q, k=k)
    return {
        "query": q,
        "results": [
            {
                "doc_id": ch.doc_id,
                "chunk_id": ch.chunk_id,
                "score": round(sc, 4),
                "text": ch.text,
            }
            for ch, sc in pairs
        ],
    }


def get_playbook(repo_root: Path) -> dict[str, Any]:
    """Read approved.md and return parsed entries."""
    pb_path = _playbook_path(repo_root)
    content = pb_path.read_text(encoding="utf-8") if pb_path.is_file() else ""
    entries = _parse_playbook_entries(content)
    return {
        "entries": entries,
        "entry_count": len(entries),
        "editable": not _is_serverless(),
        "note": (
            "On production, edits last until the next deploy. "
            "Commit knowledge/playbook/approved.md to make them permanent."
            if _is_serverless()
            else "Changes persist to the local file. Commit approved.md to deploy them."
        ),
    }


def append_playbook_entry(repo_root: Path, title: str, content: str) -> dict[str, Any]:
    """Append a new section to approved.md."""
    if _is_serverless():
        return {
            "ok": False,
            "error": (
                "Playbook edits are read-only on production. "
                "Add sections to knowledge/playbook/approved.md and re-deploy."
            ),
        }
    if not title.strip():
        return {"ok": False, "error": "Title is required."}
    if not content.strip():
        return {"ok": False, "error": "Content is required."}

    pb_path = _playbook_path(repo_root)
    pb_path.parent.mkdir(parents=True, exist_ok=True)

    heading = title.strip()
    if not heading.startswith("###"):
        heading = f"### {heading}"
    block = f"{heading}\n\n{content.strip()}"

    prev = pb_path.read_text(encoding="utf-8") if pb_path.is_file() else ""
    sep = "\n\n" if prev.strip() else ""
    pb_path.write_text(prev.rstrip() + sep + block + "\n", encoding="utf-8")

    return {"ok": True, "id": _entry_hash(block)}


def delete_playbook_entry(repo_root: Path, entry_id: str) -> dict[str, Any]:
    """Remove a playbook section by its hash ID."""
    if _is_serverless():
        return {"ok": False, "error": "Playbook edits are read-only on production."}

    pb_path = _playbook_path(repo_root)
    if not pb_path.is_file():
        return {"ok": False, "error": "Playbook file not found."}

    content = pb_path.read_text(encoding="utf-8")
    entries = _parse_playbook_entries(content)

    target = next((e for e in entries if e["id"] == entry_id), None)
    if not target:
        return {"ok": False, "error": "Entry not found."}

    # Remove the section — match heading + blank line + body
    pattern = re.escape(target["heading"]) + r"\s*\n+" + re.escape(target["body"])
    new_content, n = re.subn(pattern, "", content)
    if n == 0:
        # Fallback: remove just the heading+body verbatim
        new_content = content.replace(target["heading"] + "\n\n" + target["body"], "")

    # Collapse excess blank lines and ensure trailing newline
    new_content = re.sub(r"\n{3,}", "\n\n", new_content).strip()
    if new_content:
        new_content += "\n"
    pb_path.write_text(new_content, encoding="utf-8")

    return {"ok": True}


def ingest_raw_document(
    repo_root: Path,
    filename: str,
    markdown_content: str,
) -> dict[str, Any]:
    """Backward-compatible alias — writes to raw/hammer-data/ like the repo corpus."""
    from knowledge_ingest import ingest_hammer_raw_from_text

    return ingest_hammer_raw_from_text(
        repo_root,
        filename=filename,
        markdown_content=markdown_content,
    )
