#!/usr/bin/env python3
"""Build support_kb.sqlite from wiki-support + raw/support-data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from knowledge.chunking import chunk_markdown, strip_frontmatter  # noqa: E402
from knowledge_support.retriever import ALLOWED_WIKI_FILES, RAW_DOC_PREFIX  # noqa: E402

DEFAULT_WIKI = REPO_ROOT / "wiki-support"
DEFAULT_RAW = REPO_ROOT / "raw" / "support-data"
DEFAULT_DB = REPO_ROOT / "knowledge_support" / "data" / "support_kb.sqlite"
SCHEMA_PATH = REPO_ROOT / "knowledge" / "schema.sql"
GENERATED_DIR = REPO_ROOT / "knowledge_support" / "generated"


def _first_heading_title(md: str) -> str | None:
    for line in md.splitlines():
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _insert_document(cur, path_key: str, raw: str, kind: str, *, chunk_size: int, chunk_overlap: int) -> int:
    body = strip_frontmatter(raw)
    title = _first_heading_title(body) or Path(path_key).stem.replace("-", " ").title()
    digest = _sha256(body)
    cur.execute(
        """
        INSERT INTO kb_document (path, title, body, sha256, mtime_ns, kind)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (path_key, title, body, digest, 0, kind),
    )
    doc_id = int(cur.lastrowid)
    chunks = chunk_markdown(raw, max_chars=chunk_size, overlap=chunk_overlap)
    for idx, chunk in enumerate(chunks):
        cur.execute(
            "INSERT INTO kb_chunk (document_id, chunk_index, text) VALUES (?, ?, ?)",
            (doc_id, idx, chunk),
        )
    return len(chunks)


def sync(
    wiki_dir: Path,
    db_path: Path,
    *,
    support_raw_dir: Path | None,
    chunk_size: int,
    chunk_overlap: int,
    full_wiki: bool,
) -> dict[str, int]:
    if not wiki_dir.is_dir():
        raise SystemExit(f"Wiki directory not found: {wiki_dir}")

    stats = {"wiki_files": 0, "raw_files": 0, "files": 0, "chunks": 0}

    def build(conn: sqlite3.Connection) -> None:
        _init_schema(conn)
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()

        wiki_paths: list[Path] = []
        if full_wiki:
            wiki_paths = sorted(
                p
                for p in wiki_dir.rglob("*.md")
                if not any(part.startswith(".") for part in p.relative_to(wiki_dir).parts)
            )
        else:
            for name in ALLOWED_WIKI_FILES:
                p = wiki_dir / name
                if not p.is_file():
                    raise SystemExit(f"Missing allowlisted wiki file: {p}")
                wiki_paths.append(p)
            topics_dir = wiki_dir / "topics"
            if topics_dir.is_dir():
                wiki_paths.extend(sorted(topics_dir.rglob("*.md")))

        for path in wiki_paths:
            if path.parent == wiki_dir:
                rel = path.name
            else:
                rel = path.relative_to(wiki_dir).as_posix()
            n = _insert_document(
                cur,
                rel,
                path.read_text(encoding="utf-8", errors="replace"),
                "wiki",
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            stats["wiki_files"] += 1
            stats["files"] += 1
            stats["chunks"] += n

        if support_raw_dir and support_raw_dir.is_dir():
            root = support_raw_dir.resolve()
            for path in sorted(root.rglob("*.md")):
                if any(part.startswith(".") for part in path.relative_to(root).parts):
                    continue
                key = RAW_DOC_PREFIX + path.relative_to(root).as_posix()
                n = _insert_document(
                    cur,
                    key,
                    path.read_text(encoding="utf-8", errors="replace"),
                    "raw",
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
                stats["raw_files"] += 1
                stats["files"] += 1
                stats["chunks"] += n

        pb = REPO_ROOT / "knowledge_support" / "playbook" / "approved.md"
        if pb.is_file():
            n = _insert_document(
                cur,
                "playbook/approved.md",
                pb.read_text(encoding="utf-8", errors="replace"),
                "wiki",
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            stats["wiki_files"] += 1
            stats["files"] += 1
            stats["chunks"] += n

        cur.execute(
            """
            INSERT INTO kb_sync (id, synced_at, wiki_root, file_count, chunk_count)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              synced_at=excluded.synced_at,
              wiki_root=excluded.wiki_root,
              file_count=excluded.file_count,
              chunk_count=excluded.chunk_count
            """,
            (
                now,
                f"wiki={wiki_dir.resolve()}; raw={support_raw_dir.resolve() if support_raw_dir else 'none'}",
                stats["files"],
                stats["chunks"],
            ),
        )

        cur.execute("DELETE FROM kb_product")
        cur.executemany(
            """
            INSERT INTO kb_product (slug, name, summary, wiki_path, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("login", "Login & Access", "Password reset, OTP, account access issues.", "entity-hammer-support.md", 10),
                ("dashboard", "Hammer Dashboard", "Inbox, settings, reporting, user management.", "entity-hammer-support.md", 20),
                ("integrations", "CRM Integrations", "DealerTrack, Tekion, VinSolutions, CDK setup.", "entity-hammer-support.md", 30),
                ("facebook-aia", "Facebook AIA", "Meta ads, inventory sync, lead routing.", "entity-hammer-support.md", 40),
                ("marketposter", "MarketPoster", "Marketplace posting Chrome extension.", "entity-hammer-support.md", 50),
                ("hammer-connect", "Hammer Connect", "Marketplace messaging and SMS threads.", "entity-hammer-support.md", 60),
                ("billing", "Billing", "Subscription, renewal, payment on file.", "entity-hammer-support.md", 70),
            ],
        )

    if db_path.exists():
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".sqlite", prefix="support_kb_", dir=str(db_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        conn = sqlite3.connect(str(tmp_path))
        try:
            build(conn)
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp_path, db_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    return stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI)
    p.add_argument("--support-raw-dir", type=Path, default=DEFAULT_RAW)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--chunk-size", type=int, default=1200)
    p.add_argument("--chunk-overlap", type=int, default=150)
    p.add_argument("--full-wiki", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    stats = sync(
        args.wiki_dir.resolve(),
        args.db.resolve(),
        support_raw_dir=args.support_raw_dir.resolve() if args.support_raw_dir else None,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        full_wiki=args.full_wiki,
    )
    if args.json:
        print(json.dumps({"db": str(args.db.resolve()), **stats}))
    else:
        print(f"Indexed {stats['files']} files ({stats['chunks']} chunks) -> {args.db}")


if __name__ == "__main__":
    main()
