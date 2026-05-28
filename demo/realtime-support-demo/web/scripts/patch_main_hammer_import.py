from pathlib import Path

p = Path(__file__).resolve().parent.parent / "src" / "main.ts"
text = p.read_text(encoding="utf-8")
start = text.index("const BASE_INSTRUCTIONS = ")
end = text.index("const MODE_EXTRA", start)
replacement = (
    'import { HAMMER_SALES_INSTRUCTIONS } from "./hammer-sales-instructions";\n\n'
    "const BASE_INSTRUCTIONS = HAMMER_SALES_INSTRUCTIONS;\n\n"
)
p.write_text(text[:start] + replacement + text[end:], encoding="utf-8")
print("main.ts updated")
