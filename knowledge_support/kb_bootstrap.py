"""Ensure support_kb.sqlite exists before the support demo starts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = KNOWLEDGE_DIR.parent
DEFAULT_DB = KNOWLEDGE_DIR / "data" / "support_kb.sqlite"


def ensure_support_kb_database(
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    force: bool = False,
) -> Path:
    root = (repo_root or REPO_ROOT).resolve()
    db = (db_path or (root / "knowledge_support" / "data" / "support_kb.sqlite")).resolve()
    if db.is_file() and not force:
        return db
    script = root / "knowledge_support" / "scripts" / "sync_sqlite.py"
    if not script.is_file():
        return db
    subprocess.run([sys.executable, str(script), "--db", str(db)], cwd=str(root), check=False)
    return db
