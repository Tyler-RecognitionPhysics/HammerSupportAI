"""Publish and download support_kb.sqlite artifacts (Fly sync host → Vercel)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
SQLITE_NAME = "support_kb.sqlite"
TICKETS_STATE_NAME = "hubspot_tickets_sync.sqlite"


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _runtime_data_dir() -> Path:
    override = os.environ.get("SUPPORT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-support-demo")
    return _repo_root() / "knowledge_support" / "data"


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def artifact_data_dir() -> Path:
    return _runtime_data_dir()


def kb_db_path() -> Path:
    override = os.environ.get("SUPPORT_KB_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _runtime_data_dir() / SQLITE_NAME


def manifest_path() -> Path:
    return _runtime_data_dir() / MANIFEST_NAME


def tickets_state_path() -> Path:
    override = os.environ.get("SUPPORT_HUBSPOT_TICKETS_STATE_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _runtime_data_dir() / TICKETS_STATE_NAME


def artifact_token() -> str:
    return (
        os.environ.get("SUPPORT_KB_ARTIFACT_TOKEN", "").strip()
        or os.environ.get("SUPPORT_ADMIN_SECRET", "").strip()
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_stats(db_path: Path) -> dict[str, int]:
    stats = {"total_docs": 0, "total_chunks": 0, "ticket_docs": 0}
    if not db_path.is_file():
        return stats
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            stats["total_docs"] = int(
                conn.execute("SELECT COUNT(*) FROM kb_document").fetchone()[0]
            )
            stats["total_chunks"] = int(
                conn.execute("SELECT COUNT(*) FROM kb_chunk").fetchone()[0]
            )
            stats["ticket_docs"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM kb_document WHERE path LIKE '%hubspot-tickets%'"
                ).fetchone()[0]
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning("sqlite stats failed: %s", exc)
    return stats


def write_manifest(*, ticket_count: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    db = kb_db_path()
    if not db.is_file():
        raise FileNotFoundError(f"KB database not found: {db}")

    stats = _sqlite_stats(db)
    if ticket_count is None:
        ticket_count = stats["ticket_docs"]

    manifest: dict[str, Any] = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256_file(db),
        "size_bytes": db.stat().st_size,
        "ticket_count": int(ticket_count or 0),
        "total_docs": stats["total_docs"],
        "total_chunks": stats["total_chunks"],
    }
    if extra:
        manifest.update(extra)

    dest = manifest_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_manifest() -> dict[str, Any] | None:
    path = manifest_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("manifest read failed: %s", exc)
        return None


def _artifact_headers() -> dict[str, str]:
    token = artifact_token()
    headers = {"User-Agent": "hammer-support-kb-bootstrap/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _download_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name}"


def _http_get(url: str, *, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers=_artifact_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download_artifacts(*, dest_dir: Path) -> dict[str, Any]:
    """Download manifest + sqlite (+ tickets state) from SUPPORT_KB_ARTIFACT_URL."""
    base = os.environ.get("SUPPORT_KB_ARTIFACT_URL", "").strip().rstrip("/")
    if not base:
        return {"ok": False, "skipped": True, "reason": "SUPPORT_KB_ARTIFACT_URL not set"}

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_db = dest_dir / SQLITE_NAME

    try:
        manifest_raw = _http_get(_download_url(base, MANIFEST_NAME), timeout=30.0)
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        _log.warning("artifact manifest download failed: %s", exc)
        return {"ok": False, "error": f"manifest download failed: {exc}"}

    expected_sha = str(manifest.get("sha256") or "")
    if dest_db.is_file() and expected_sha and sha256_file(dest_db) == expected_sha:
        _log.info("support_kb.sqlite already current (sha256 match)")
    else:
        try:
            db_bytes = _http_get(_download_url(base, SQLITE_NAME), timeout=300.0)
        except (urllib.error.URLError, TimeoutError) as exc:
            _log.warning("artifact sqlite download failed: %s", exc)
            return {"ok": False, "error": f"sqlite download failed: {exc}"}

        if expected_sha:
            actual = hashlib.sha256(db_bytes).hexdigest()
            if actual != expected_sha:
                return {"ok": False, "error": "sqlite sha256 mismatch after download"}

        dest_db.write_bytes(db_bytes)
        _log.info("Downloaded support_kb.sqlite (%s bytes)", len(db_bytes))

    (dest_dir / MANIFEST_NAME).write_bytes(manifest_raw)

    tickets_dest = dest_dir / TICKETS_STATE_NAME
    try:
        state_bytes = _http_get(_download_url(base, TICKETS_STATE_NAME), timeout=60.0)
        if state_bytes[:16].startswith(b"SQLite format"):
            tickets_dest.write_bytes(state_bytes)
    except (urllib.error.URLError, TimeoutError):
        _log.info("tickets state artifact not available (optional)")

    return {
        "ok": True,
        "manifest": manifest,
        "dest_db": str(dest_db),
        "size_bytes": dest_db.stat().st_size if dest_db.is_file() else 0,
    }
