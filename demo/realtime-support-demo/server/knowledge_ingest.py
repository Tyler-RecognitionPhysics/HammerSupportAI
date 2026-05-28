"""Ingest uploaded sources into raw/hammer-data/ — same corpus the voice retriever indexes."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_RAW_SUBDIR = "dashboard-uploads"

_TEXT_EXTENSIONS = frozenset({".txt", ".text", ".md", ".markdown"})
_PDF_EXTENSIONS = frozenset({".pdf"})


def _is_serverless() -> bool:
    return os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def hammer_raw_dir(repo_root: Path) -> Path:
    return (repo_root / "raw" / "hammer-data").resolve()


def ingest_editable() -> bool:
    return not _is_serverless()


def _sanitize_stem(name: str) -> str:
    stem = Path(name).stem.strip()
    stem = re.sub(r"[^\w\s\-().]+", "-", stem, flags=re.UNICODE)
    stem = re.sub(r"[\s_]+", "-", stem).strip("-")
    return stem or "upload"


def _ensure_markdown_title(content: str, title: str) -> str:
    text = content.strip()
    if not text:
        return text
    if text.lstrip().startswith("#"):
        return text + ("\n" if not text.endswith("\n") else "")
    safe_title = title.strip() or "Uploaded document"
    return f"# {safe_title}\n\n{text}\n"


def pdf_bytes_to_markdown(data: bytes, *, title: str | None = None) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support requires pypdf on the server") from exc

    import io

    reader = PdfReader(io.BytesIO(data))
    doc_title = title or "Uploaded PDF"
    lines = [f"# {doc_title}", ""]

    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        text = text.replace("\uFFFD", "'")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
        if not blocks:
            blocks = [text]
        for block in blocks:
            if len(block) < 100 and (block.endswith("?") or block.isupper()):
                lines.append(f"## {block}")
            else:
                lines.append(block)
            lines.append("")

    body = "\n".join(lines).strip()
    return (body + "\n") if body else f"# {doc_title}\n\n_(No extractable text in PDF.)_\n"


def convert_upload_to_markdown(
    *,
    filename: str,
    data: bytes,
    title: str | None = None,
) -> tuple[str, str]:
    """Return (markdown_text, output_basename_without_dir).

    Output names follow the hammer-data convention, e.g. ``MyDeck.pdf.md``.
    """
    name = (filename or "upload").strip()
    suffix = Path(name).suffix.lower()
    stem = _sanitize_stem(name)
    display_title = (title or Path(name).stem or stem).strip()

    if suffix in _PDF_EXTENSIONS:
        md = pdf_bytes_to_markdown(data, title=f"{Path(name).stem}.pdf")
        return md, f"{stem}.pdf.md"

    if suffix in _TEXT_EXTENSIONS or not suffix:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        if suffix in {".md", ".markdown"}:
            md = text if text.lstrip().startswith("#") else _ensure_markdown_title(text, display_title)
        else:
            md = _ensure_markdown_title(text, display_title)
        out_name = f"{stem}.md" if suffix not in {".md", ".markdown"} else f"{stem}{suffix}"
        if not out_name.endswith(".md"):
            out_name = f"{stem}.md"
        return md.strip() + "\n", out_name

    raise ValueError(f"Unsupported file type: {suffix or '(none)'}. Use PDF, Markdown, or plain text.")


def ingest_hammer_raw_markdown(
    repo_root: Path,
    *,
    markdown: str,
    output_name: str,
    subdir: str = _RAW_SUBDIR,
) -> dict[str, Any]:
    """Write markdown under raw/hammer-data/ for BM25 retrieval (same as repo corpus)."""
    if not ingest_editable():
        return {
            "ok": False,
            "error": (
                "Raw ingestion is read-only on production. "
                "Add files under raw/hammer-data/ in git and re-deploy."
            ),
        }

    content = (markdown or "").strip()
    if not content:
        return {"ok": False, "error": "Content is empty after conversion."}

    safe_name = re.sub(r"[^a-zA-Z0-9_\-.]", "-", output_name.strip())
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{safe_name}.md"

    dest_dir = hammer_raw_dir(repo_root) / subdir.strip("/\\")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        n = 2
        while dest.exists():
            dest = dest_dir / f"{stem}-{n}{suffix}"
            n += 1

    dest.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    rel = dest.relative_to(repo_root).as_posix()
    doc_id = "raw/hammer-data/" + dest.relative_to(hammer_raw_dir(repo_root)).as_posix()
    return {
        "ok": True,
        "path": rel,
        "doc_id": doc_id,
        "kind": "raw",
        "filename": dest.name,
    }


def ingest_hammer_raw_upload(
    repo_root: Path,
    *,
    filename: str,
    data: bytes,
    title: str | None = None,
) -> dict[str, Any]:
    try:
        markdown, output_name = convert_upload_to_markdown(
            filename=filename,
            data=data,
            title=title,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Conversion failed: {exc}"}

    return ingest_hammer_raw_markdown(
        repo_root,
        markdown=markdown,
        output_name=output_name,
    )


def ingest_hammer_raw_from_text(
    repo_root: Path,
    *,
    filename: str,
    markdown_content: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Paste path — treat content as markdown/text and store under raw/hammer-data/."""
    name = (filename or "upload").strip()
    stem = _sanitize_stem(name)
    display_title = (title or stem).strip()
    md = markdown_content.strip()
    if not md:
        return {"ok": False, "error": "Content is required."}
    if not md.lstrip().startswith("#"):
        md = _ensure_markdown_title(md, display_title)
    out_name = f"{stem}.md" if not name.lower().endswith(".md") else stem + ".md"
    if name.lower().endswith(".pdf.md"):
        out_name = f"{stem}.pdf.md"
    return ingest_hammer_raw_markdown(repo_root, markdown=md, output_name=out_name)
