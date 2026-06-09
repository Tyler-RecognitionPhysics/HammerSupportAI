"""Ensure support_kb.sqlite exists before the support demo starts."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = KNOWLEDGE_DIR.parent
DEFAULT_DB = KNOWLEDGE_DIR / "data" / "support_kb.sqlite"


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _copy_state_databases(src_dir: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in ("hubspot_tickets_sync.sqlite", "hubspot_kb_sync.sqlite", "slack_sync.sqlite"):
        src = src_dir / name
        dest = dest_dir / name
        if src.is_file() and (not dest.is_file() or src.stat().st_size != dest.stat().st_size):
            shutil.copy2(src, dest)


def ensure_support_kb_database(
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    force: bool = False,
) -> Path:
    root = (repo_root or REPO_ROOT).resolve()
    db = (db_path or (root / "knowledge_support" / "data" / "support_kb.sqlite")).resolve()
    bundled_db = (root / "knowledge_support" / "data" / "support_kb.sqlite").resolve()

    if db.is_file() and not force:
        if _is_serverless() and os.environ.get("SUPPORT_KB_ARTIFACT_URL", "").strip():
            pass
        elif bundled_db.is_file() and db != bundled_db and bundled_db.stat().st_size != db.stat().st_size:
            pass
        else:
            return db

    if _is_serverless() and os.environ.get("SUPPORT_KB_ARTIFACT_URL", "").strip():
        from knowledge_support.kb_artifact import download_artifacts

        result = download_artifacts(dest_dir=db.parent)
        if result.get("ok"):
            _log.info(
                "Loaded KB artifact: %s tickets, %s chunks",
                result.get("manifest", {}).get("ticket_count"),
                result.get("manifest", {}).get("total_chunks"),
            )
            return db
        _log.warning("Artifact download failed, falling back to bundled copy: %s", result.get("error"))

    if db != bundled_db and bundled_db.is_file() and not force:
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_db, db)
        _copy_state_databases(bundled_db.parent, db.parent)
        return db

    script = root / "knowledge_support" / "scripts" / "sync_sqlite.py"
    if not script.is_file():
        return db
    subprocess.run([sys.executable, str(script), "--db", str(db)], cwd=str(root), check=False)
    return db
