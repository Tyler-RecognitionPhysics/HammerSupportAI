"""Ingest uploaded sources into raw/support-data/dashboard-uploads/."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_RAW_SUBDIR = "dashboard-uploads"


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def support_raw_dir(repo_root: Path) -> Path:
    override = os.environ.get("SUPPORT_RAW_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (repo_root / "raw" / "support-data").resolve()


def ingest_editable() -> bool:
    return not _is_serverless()


def _sanitize_stem(name: str) -> str:
    stem = Path(name).stem.strip()
    stem = re.sub(r"[^\w\s\-().]+", "-", stem, flags=re.UNICODE)
    stem = re.sub(r"[\s_]+", "-", stem).strip("-")
    return stem or "upload"


def ingest_support_raw_from_text(
    repo_root: Path,
    *,
    filename: str,
    markdown_content: str,
) -> dict[str, Any]:
    if not ingest_editable():
        return {"ok": False, "error": "Uploads are read-only on production."}
    raw_root = support_raw_dir(repo_root)
    dest_dir = raw_root / _RAW_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = _sanitize_stem(filename)
    out = dest_dir / f"{stem}.md"
    if not markdown_content.lstrip().startswith("#"):
        markdown_content = f"# {stem.replace('-', ' ').title()}\n\n{markdown_content.strip()}\n"
    out.write_text(markdown_content, encoding="utf-8")
    return {"ok": True, "path": str(out.relative_to(repo_root)).replace("\\", "/")}
