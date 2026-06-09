"""Pinned ticket associations — map a question/topic to a specific HubSpot ticket.

Operators testing Hannah in the Knowledge tab can "pin" a resolved ticket to the
question they just asked. The retriever then force-includes that ticket for the
same question and closely related ones, so the best reference case is always used.

Storage is a small JSON file alongside the playbook (writable locally, read-only
on serverless). The retriever reads the same file to apply the associations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def pins_path(repo_root: Path) -> Path:
    override = os.environ.get("SUPPORT_TICKET_PINS_JSON", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (repo_root / "knowledge_support" / "playbook" / "ticket_pins.json").resolve()


def _keywords(topic: str) -> list[str]:
    seen: dict[str, None] = {}
    for tok in _TOKEN_RE.findall((topic or "").lower()):
        if len(tok) > 1:
            seen.setdefault(tok, None)
    return list(seen.keys())


def _pin_id(topic: str, doc_id: str) -> str:
    raw = f"{(topic or '').strip().lower()}::{(doc_id or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def load_pins(repo_root: Path) -> list[dict[str, Any]]:
    path = pins_path(repo_root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pins = data.get("pins") if isinstance(data, dict) else data
    return pins if isinstance(pins, list) else []


def _write_pins(path: Path, pins: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pins": pins}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def add_pin(
    repo_root: Path,
    *,
    topic: str,
    ticket_doc_id: str,
    ticket_id: str = "",
    ticket_url: str = "",
    title: str = "",
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    if _is_serverless():
        return {"ok": False, "error": "Ticket pins are read-only on production."}
    topic = (topic or "").strip()
    ticket_doc_id = (ticket_doc_id or "").strip()
    if not topic:
        return {"ok": False, "error": "A question/topic is required."}
    if not ticket_doc_id:
        return {"ok": False, "error": "A ticket is required."}

    path = pins_path(repo_root)
    pins = load_pins(repo_root)
    pin_id = _pin_id(topic, ticket_doc_id)
    kw = list(dict.fromkeys((keywords or []) + _keywords(topic)))
    record = {
        "id": pin_id,
        "topic": topic,
        "keywords": kw,
        "ticket_doc_id": ticket_doc_id,
        "ticket_id": str(ticket_id or ""),
        "ticket_url": str(ticket_url or ""),
        "title": str(title or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pins = [p for p in pins if p.get("id") != pin_id]
    pins.append(record)
    _write_pins(path, pins)
    return {"ok": True, "pin": record}


def delete_pin(repo_root: Path, pin_id: str) -> dict[str, Any]:
    if _is_serverless():
        return {"ok": False, "error": "Ticket pins are read-only on production."}
    pin_id = (pin_id or "").strip()
    if not pin_id:
        return {"ok": False, "error": "Pin id required."}
    path = pins_path(repo_root)
    pins = load_pins(repo_root)
    remaining = [p for p in pins if p.get("id") != pin_id]
    if len(remaining) == len(pins):
        return {"ok": False, "error": "Pin not found."}
    _write_pins(path, remaining)
    return {"ok": True}


def list_pins(repo_root: Path) -> dict[str, Any]:
    pins = sorted(load_pins(repo_root), key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return {"ok": True, "pins": pins, "editable": not _is_serverless()}
