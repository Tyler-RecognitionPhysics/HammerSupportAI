"""Load keyed UI strings from wiki/demo-public-site-copy.md (single source for demo pages)."""

from __future__ import annotations

import re
from pathlib import Path

SITE_COPY_FILENAME = "demo-public-site-copy.md"


def _strip_frontmatter(md: str) -> str:
    if not md.startswith("---"):
        return md
    parts = md.split("---", 2)
    if len(parts) >= 3 and parts[0].strip() == "":
        return parts[2].lstrip("\n")
    return md


def parse_site_copy_markdown(md: str) -> dict[str, str]:
    """Parse `## key` sections into a flat string map."""
    body = _strip_frontmatter(md)
    out: dict[str, str] = {}
    current_key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current_key, buf
        if current_key is not None:
            text = "\n".join(buf).strip()
            if text:
                out[current_key] = text
        buf = []

    for line in body.splitlines():
        m = re.match(r"^##\s+([a-z0-9_]+)\s*$", line.strip(), re.I)
        if m:
            flush()
            current_key = m.group(1).lower()
            continue
        if current_key is not None:
            buf.append(line.rstrip())

    flush()
    return out


def load_site_copy(wiki_dir: Path) -> dict[str, str]:
    path = Path(wiki_dir) / SITE_COPY_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"Missing wiki site copy file: {path}")
    return parse_site_copy_markdown(path.read_text(encoding="utf-8"))
