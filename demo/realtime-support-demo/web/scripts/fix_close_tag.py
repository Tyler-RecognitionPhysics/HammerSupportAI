#!/usr/bin/env python3
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "src" / "main.ts"
t = p.read_text(encoding="utf-8")
t = t.replace("      </motion>`;", "      </div>`;", 1)
p.write_text(t, encoding="utf-8")
print("fixed:", "      </motion>`;" not in t)
