#!/usr/bin/env python3
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "src" / "main.ts"
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
out = []
skip = False
for line in lines:
    if 'class="hero-glass__sms"' in line:
        skip = True
        continue
    if skip:
        if 'class="hero-glass__panel' in line:
            skip = False
            out.append(line)
        continue
    out.append(line)

text = "".join(out)
# Remove glass tab listener block
old_listener = """    root.querySelectorAll<HTMLButtonElement>(".hero-glass__tool[data-glass-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.glassTab;
        root.querySelectorAll(".hero-glass__tool[data-glass-tab]").forEach((b) => {
          const on = b === btn;
          b.classList.toggle("is-active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        const sms = root.querySelector("#glassSms");
        if (sms) sms.classList.toggle("is-hidden", tab !== "sms");
      });
    });
"""
text = text.replace(old_listener, "")

p.write_text(text, encoding="utf-8")
print("removed sms block")
