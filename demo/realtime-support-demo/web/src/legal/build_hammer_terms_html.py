"""One-off generator: uploads/terms-0.md -> legal/hammer-terms-fragment.html"""
from __future__ import annotations

import html
import re
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve().parent
    md_path = here / "hammer-terms-source.md"
    if not md_path.is_file():
        md_path = Path(
            r"C:\Users\tbenn\.cursor\projects\c-Users-tbenn-Desktop-Voice-VibeVoice\uploads\terms-0.md"
        )
    out_path = here / "hammer-terms-fragment.html"
    text = md_path.read_text(encoding="utf-8")
    start = text.index("## Terms Of Use")
    end = text.index("Copy of Copy of Dealerrefresh", start)
    chunk = text[start:end].strip()
    lines = chunk.splitlines()
    body_lines: list[str] = []
    seen_last = False
    for ln in lines:
        if ln.strip() == "## Terms Of Use":
            continue
        if ln.startswith("Last updated:"):
            seen_last = True
            body_lines.append(ln.strip())
            continue
        if not seen_last:
            continue
        body_lines.append(ln)
    body = "\n".join(body_lines).strip()

    def md_links(s: str) -> str:
        def repl(m: re.Match[str]) -> str:
            t, u = m.group(1), m.group(2)
            return (
                f'<a href="{html.escape(u, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{html.escape(t)}</a>'
            )

        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl, s)

    def strip_md_escapes(s: str) -> str:
        return s.replace("\\_", "_").replace("\\.", ".")

    anchor_re = re.compile(r"(<a\b[^>]*>.*?</a>)", re.DOTALL)
    http_re = re.compile(r'(?<![">])(https?://[^\s<"]+)')
    www_re = re.compile(r'(?<![">/])(www\.[^\s<")]+)')

    def trim_trailing_punct(u: str) -> tuple[str, str]:
        tail = ""
        while u and u[-1] in ".,;:!?)\"'":
            tail = u[-1] + tail
            u = u[:-1]
        return u, tail

    def link_http(s: str) -> str:
        def repl(m: re.Match[str]) -> str:
            raw = m.group(1)
            u, tail = trim_trailing_punct(raw)
            if not u:
                return m.group(0)
            return (
                f'<a href="{html.escape(u, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{html.escape(u)}</a>{tail}'
            )

        return http_re.sub(repl, s)

    def link_www(s: str) -> str:
        def repl(m: re.Match[str]) -> str:
            raw = m.group(0)
            u, tail = trim_trailing_punct(raw)
            full = "https://" + u
            return (
                f'<a href="{html.escape(full, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{html.escape(u)}</a>{tail}'
            )

        return www_re.sub(repl, s)

    blocks = re.split(r"\n{2,}", body)
    out: list[str] = []
    out.append(
        '<div class="nav-panel__legal nav-panel__legal--full-terms '
        'nav-panel__legal--hammer-terms">'
    )

    for b in blocks:
        b = b.strip()
        if not b:
            continue
        m3 = re.match(r"^####\s+(\d+)\\\.\s+(.+)$", b)
        if not m3:
            m3 = re.match(r"^####\s+(\d+)\.\s+(.+)$", b)
        if m3:
            out.append(
                f'<h3 class="nav-panel__legal-h">{html.escape(m3.group(1))}. '
                f"{html.escape(strip_md_escapes(m3.group(2)))}</h3>"
            )
            continue
        m4 = re.match(r"^#####\s+(.+)$", b)
        if m4:
            out.append(
                f'<h4 class="nav-panel__legal-subh">'
                f"{html.escape(strip_md_escapes(m4.group(1)))}</h4>"
            )
            continue
        if b.startswith("Last updated:"):
            rest = strip_md_escapes(b.removeprefix("Last updated:").strip())
            out.append(
                '<p class="nav-panel__legal-meta"><strong>Last updated:</strong> '
                f"{html.escape(rest)}</p>"
            )
            continue

        raw = strip_md_escapes(b)
        raw = md_links(raw)
        raw = re.sub(r"<(https?://[^>\s]+)>", r"\1", raw)

        parts = anchor_re.split(raw)
        seg: list[str] = []
        for p in parts:
            if p.startswith("<a "):
                seg.append(p)
            else:
                chunk = html.escape(p.replace("\n", " "))
                chunk = link_http(chunk)
                chunk = link_www(chunk)
                seg.append(chunk)
        inner = "".join(seg)

        cls = "nav-panel__legal-caps" if inner.isupper() or (
            len(inner) > 80 and sum(1 for c in inner if c.isupper()) / max(len(inner), 1) > 0.55
        ) else ""
        if cls:
            out.append(f'<p class="{cls}">{inner}</p>')
        else:
            out.append(f"<p>{inner}</p>")

    out.append("</div>")
    out_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("Wrote", out_path, "bytes", out_path.stat().st_size)


if __name__ == "__main__":
    main()
