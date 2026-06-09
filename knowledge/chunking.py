"""Markdown chunking for wiki + raw corpora (heading-aware, overlap)."""

from __future__ import annotations

import re


def strip_frontmatter(md: str) -> str:
    if not md.startswith("---"):
        return md
    parts = md.split("---", 2)
    if len(parts) >= 3 and parts[0].strip() == "":
        return parts[2].lstrip("\n")
    return md


def _chunk_by_chars(text: str, max_chars: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + max_chars)
        if end < n:
            cut = text.rfind("\n\n", start + max_chars // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + max_chars // 2, end)
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(0, end - overlap)
    return chunks


def chunk_markdown(md: str, *, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """Split markdown into retrieval-sized chunks, preferring section boundaries."""
    text = strip_frontmatter(md).strip()
    if not text:
        return []
    sections = re.split(r"\n(?=#{1,4}\s)", text)
    if len(sections) <= 1:
        return _chunk_by_chars(text, max_chars, overlap)

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= max_chars:
            chunks.append(section)
        else:
            chunks.extend(_chunk_by_chars(section, max_chars, overlap))
    return chunks
