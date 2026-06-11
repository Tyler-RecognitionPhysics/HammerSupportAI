"""Vendor / feed-provider list: storage, admin CRUD, and AI grounding.

The list lives in a JSON file — on the persistent Fly host it sits on the
volume (SUPPORT_VENDORS_JSON=/data/vendors.json) so dashboard edits survive
redeploys. Serverless (Vercel) instances never write: their admin requests are
proxied to Fly, and the AI reads through a TTL-cached remote fetch with the
bundled repo file as fallback.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

_SERVER_DIR = Path(__file__).resolve().parent

VENDOR_FIELDS = ("name", "supported", "country", "integration", "status", "notes")
SUPPORTED_LABELS = (
    "Yes",
    "No",
    "Pending Confirmation",
    "Pending",
    "No Longer Available",
    "Charges Fees to Get Feed",
)

_lock = threading.Lock()
_file_cache: dict[str, Any] = {"mtime": None, "path": None, "vendors": None}
_remote_cache: dict[str, Any] = {"at": 0.0, "vendors": None}
_REMOTE_TTL_SECONDS = 300.0


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _SERVER_DIR.parents[2]


def _vendors_path() -> Path:
    override = os.environ.get("SUPPORT_VENDORS_JSON", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / "knowledge_support" / "vendors" / "vendors.json"


def _bundled_path() -> Path:
    return _repo_root() / "knowledge_support" / "vendors" / "vendors.json"


def _read_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    vendors = data.get("vendors") if isinstance(data, dict) else data
    return [v for v in vendors if isinstance(v, dict)] if isinstance(vendors, list) else []


def _load_local(*, allow_bundled_fallback: bool = True) -> list[dict[str, Any]]:
    path = _vendors_path()
    if not path.is_file() and allow_bundled_fallback:
        path = _bundled_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if _file_cache["vendors"] is not None and _file_cache["mtime"] == mtime and _file_cache["path"] == str(path):
        return list(_file_cache["vendors"])
    vendors = _read_file(path)
    _file_cache.update(mtime=mtime, path=str(path), vendors=vendors)
    return list(vendors)


def _write_local(vendors: list[dict[str, Any]]) -> None:
    path = _vendors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"vendors": vendors}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _file_cache.update(mtime=None, path=None, vendors=None)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "vendor"


def _clean(body: dict[str, Any]) -> dict[str, str]:
    return {k: str(body.get(k) or "").strip() for k in VENDOR_FIELDS}


# --- Admin CRUD (runs on the persistent host; serverless proxies here) -------


def list_vendors() -> dict[str, Any]:
    vendors = _load_local()
    vendors.sort(key=lambda v: str(v.get("name") or "").lower())
    return {"vendors": vendors, "vendor_count": len(vendors)}


def create_vendor(body: dict[str, Any]) -> dict[str, Any]:
    fields = _clean(body)
    if not fields["name"]:
        return {"ok": False, "error": "Vendor name is required."}
    with _lock:
        vendors = _load_local()
        base = _slugify(fields["name"])
        vendor_id = base
        n = 2
        existing = {str(v.get("id")) for v in vendors}
        while vendor_id in existing:
            vendor_id = f"{base}-{n}"
            n += 1
        vendor = {"id": vendor_id, **fields}
        vendors.append(vendor)
        _write_local(vendors)
    return {"ok": True, "vendor": vendor}


def update_vendor(vendor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        vendors = _load_local()
        target = next((v for v in vendors if str(v.get("id")) == vendor_id), None)
        if not target:
            return {"ok": False, "error": "Vendor not found."}
        fields = _clean(body)
        if not fields["name"]:
            return {"ok": False, "error": "Vendor name is required."}
        target.update(fields)
        _write_local(vendors)
    return {"ok": True, "vendor": target}


def delete_vendor(vendor_id: str) -> dict[str, Any]:
    with _lock:
        vendors = _load_local()
        remaining = [v for v in vendors if str(v.get("id")) != vendor_id]
        if len(remaining) == len(vendors):
            return {"ok": False, "error": "Vendor not found."}
        _write_local(remaining)
    return {"ok": True}


def replace_vendors(vendors: list[dict[str, Any]]) -> dict[str, Any]:
    """Bulk import (seeding). Replaces the whole list."""
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for v in vendors:
        if not isinstance(v, dict) or not str(v.get("name") or "").strip():
            continue
        fields = _clean(v)
        vendor_id = str(v.get("id") or "").strip() or _slugify(fields["name"])
        base = vendor_id
        n = 2
        while vendor_id in seen:
            vendor_id = f"{base}-{n}"
            n += 1
        seen.add(vendor_id)
        cleaned.append({"id": vendor_id, **fields})
    with _lock:
        _write_local(cleaned)
    return {"ok": True, "vendor_count": len(cleaned)}


# --- AI grounding -------------------------------------------------------------


def prewarm_vendors() -> int:
    """Warm the vendor cache (on serverless this is the remote fetch from the
    persistent host) so the first vendor question doesn't pay for it."""
    return len(_vendors_for_ai())


def _vendors_for_ai() -> list[dict[str, Any]]:
    """Serverless reads the live list from the persistent host (TTL cache);
    everything else reads the local file."""
    if _is_serverless():
        now = time.time()
        cached = _remote_cache["vendors"]
        if cached is not None and now - float(_remote_cache["at"] or 0) < _REMOTE_TTL_SECONDS:
            return cached
        try:
            from support_remote_dashboard import internal_token, remote_dashboard_enabled, sync_host_url

            if remote_dashboard_enabled():
                import httpx

                resp = httpx.get(
                    f"{sync_host_url()}/api/admin/support/vendors",
                    headers={"Authorization": f"Bearer {internal_token()}"},
                    timeout=httpx.Timeout(10.0, connect=5.0),
                )
                if resp.status_code < 400:
                    vendors = resp.json().get("vendors") or []
                    _remote_cache.update(at=now, vendors=vendors)
                    return vendors
        except Exception:
            pass
        if cached is not None:
            return cached
    return _load_local()


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


_GENERIC_VENDOR_RE = re.compile(
    r"\bvendors?\b|\bfeed provider\b|\bfeed providers\b|\binventory (?:provider|feed|source)\b"
    r"|\bwork with\b|\bintegrat\w*\b|\bcompatible\b|\bsupported provider\b|\bdms\b|\bwebsite provider\b",
    re.IGNORECASE,
)


def _match_name(vendor: dict[str, Any], norm_query: str) -> bool:
    name = str(vendor.get("name") or "")
    # Match the full name and the part before any parenthetical/separator.
    candidates = {name, re.split(r"[(\[/—]| - ", name)[0]}
    for cand in candidates:
        norm = _normalize(cand)
        if len(norm) >= 3 and norm in norm_query:
            return True
    return False


def _vendor_verdict(vendor: dict[str, Any]) -> str:
    """One unambiguous customer-facing sentence: does Hammer work with them or not."""
    supported = str(vendor.get("supported") or "").strip().lower()
    status = str(vendor.get("status") or "").strip().lower()
    if supported == "yes":
        return "YES — Hammer works with this vendor."
    if supported == "no" or "cannot use" in status:
        return "NO — Hammer does not work with this vendor."
    if supported.startswith("pending"):
        return "NOT CONFIRMED YET — say support for this vendor is pending confirmation and offer to log a ticket."
    if "no longer" in supported:
        return "NO — Hammer no longer works with this vendor."
    if "fee" in supported:
        return "YES — Hammer works with this vendor, but the vendor charges a fee on their side."
    return f"Status: {vendor.get('supported') or 'Unknown'} — if unclear, offer to log a ticket to confirm."


def vendor_context_block(query: str, *, max_chars: int = 2200) -> str:
    """Authoritative vendor data for the prompt. Returns '' for unrelated turns.

    Specific vendor mentioned -> simple works-with verdict for the matches.
    Generic vendor question   -> count + supported-vendor name list (capped).
    """
    q = (query or "").strip()
    if not q:
        return ""
    vendors = _vendors_for_ai()
    if not vendors:
        return ""

    norm_query = _normalize(q)
    matches = [v for v in vendors if _match_name(v, norm_query)]
    generic = bool(_GENERIC_VENDOR_RE.search(q))
    if not matches and not generic:
        return ""

    header = (
        "## Vendor & feed provider list (authoritative — overrides every other source)\n"
        "This is Hammer's official list of inventory/feed/website vendors. For ANY question about "
        "whether Hammer works with, supports, or integrates with a vendor, answer ONLY from this data, "
        "even if tickets or other context above say something different. Keep the answer simple: just "
        "whether Hammer works with that vendor. Do NOT mention APIs, API setup, integration methods, "
        "feed mechanics, or any internal technical details — that is unnecessary for the customer. "
        "If a vendor is not in this list, say you're not sure and offer to log a ticket to confirm.\n"
    )

    if matches:
        lines = [f"- {v.get('name')}: {_vendor_verdict(v)}" for v in matches[:6]]
        return (header + "\n".join(lines))[:max_chars]

    supported = sorted(
        (str(v.get("name") or "") for v in vendors if str(v.get("supported") or "").lower() == "yes"),
        key=str.lower,
    )
    summary = (
        f"Hammer currently tracks {len(vendors)} vendors; {len(supported)} are supported feed providers. "
        "If the customer asks about a specific vendor, look it up in this list.\n"
        "Supported vendors: " + ", ".join(supported)
    )
    return (header + summary)[:max_chars]
