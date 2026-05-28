"""hammer-privacy-source.md -> hammer-privacy-fragment.html (mirrors hammertime.com/privacy)."""
from __future__ import annotations

import html
import re
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve().parent
    md_path = here / "hammer-privacy-source.md"
    out_path = here / "hammer-privacy-fragment.html"
    text = md_path.read_text(encoding="utf-8")
    start = text.index("## Privacy")
    end = text.index("bottom of page", start)
    chunk = text[start:end].strip()
    chunk = re.sub(r"^## Privacy\s*\n+", "", chunk)
    body = chunk.strip()

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

    def format_block(raw_block: str) -> str:
        raw = strip_md_escapes(raw_block)
        raw = md_links(raw)
        raw = re.sub(r"<(https?://[^>\s]+)>", r"\1", raw)
        if "Attention: Hammer Corp" in raw and "\n" in raw:
            parts_addr = anchor_re.split(raw)
            seg_addr: list[str] = []
            for p in parts_addr:
                if p.startswith("<a "):
                    seg_addr.append(p)
                else:
                    chunks: list[str] = []
                    for line in p.strip().split("\n"):
                        if not line.strip():
                            continue
                        line_e = html.escape(line.strip())
                        line_e = link_http(line_e)
                        line_e = link_www(line_e)
                        chunks.append(line_e)
                    seg_addr.append("<br>".join(chunks))
            return f"<p>{''.join(seg_addr)}</p>"

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
        return "".join(seg)

    blocks = re.split(r"\n{2,}", body)
    out: list[str] = []
    out.append(
        '<div class="nav-panel__legal nav-panel__legal--full-terms '
        'nav-panel__legal--hammer-privacy">'
    )

    for b in blocks:
        b = b.strip()
        if not b:
            continue

        m_meta = re.match(r"^Last Updated:\s*(.+)$", b, re.I)
        if m_meta and "\n" not in b:
            rest = strip_md_escapes(m_meta.group(1).strip())
            out.append(
                '<p class="nav-panel__legal-meta"><strong>Last Updated:</strong> '
                f"{html.escape(rest)}</p>"
            )
            continue

        m_sec = re.match(r"^###\s+(\d+)\.\s+(.+)$", b)
        if m_sec and "\n" not in b:
            out.append(
                f'<h3 class="nav-panel__legal-h">{html.escape(m_sec.group(1))}. '
                f"{html.escape(strip_md_escapes(m_sec.group(2)))}</h3>"
            )
            continue

        if b.startswith("#### "):
            parts = b.split("\n", 1)
            title = strip_md_escapes(parts[0].removeprefix("####").strip())
            out.append(f'<h4 class="nav-panel__legal-subh">{html.escape(title)}</h4>')
            if len(parts) > 1 and parts[1].strip():
                inner = format_block(parts[1].strip())
                if inner.startswith("<p>"):
                    out.append(inner)
                else:
                    out.append(f"<p>{inner}</p>")
            continue

        if b == "Hammer Corp Privacy Policy" or b.strip() == "Hammer Corp Privacy Policy":
            out.append(
                '<p class="nav-panel__legal-brand"><strong>'
                "Hammer Corp Privacy Policy</strong></p>"
            )
            continue

        inner = format_block(b)
        if inner.startswith("<p>"):
            out.append(inner)
        else:
            out.append(f"<p>{inner}</p>")

    out.append("</div>")
    out_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("Wrote", out_path, "bytes", out_path.stat().st_size)


if __name__ == "__main__":
    main()
