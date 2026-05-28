"""One-off helper: sync HAMMER_SALES_INSTRUCTIONS from main.ts BASE_INSTRUCTIONS block."""
from pathlib import Path

root = Path(__file__).resolve().parent.parent / "src"
text = (root / "main.ts").read_text(encoding="utf-8")
marker = 'const BASE_INSTRUCTIONS = `${""}\n\n'
start = text.index(marker) + len(marker)
end = text.index("`;\n\nconst MODE_EXTRA", start)
body = text[start:end]
out = root / "hammer-sales-instructions.ts"
out.write_text(
    "/**\n"
    " * Hammer product sales voice prompt — browser WebRTC / ElevenLabs live demo.\n"
    " * Phone (SIP) uses pen-challenge-instructions.ts instead.\n"
    " */\n"
    f"export const HAMMER_SALES_INSTRUCTIONS = `{body}`;\n",
    encoding="utf-8",
)
print(f"wrote {out} ({len(body)} chars)")
