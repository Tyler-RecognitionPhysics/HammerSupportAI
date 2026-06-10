"""Seed the Fly volume playbook (/data/playbook/approved.md) from the repo file.

Usage: set ADMIN_SECRET env var, then: py -3 scripts/seed_fly_playbook.py
Idempotent-ish: skips entries whose heading already exists on the host.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HOST = "https://hammer-support-sync.fly.dev"
SECRET = os.environ["ADMIN_SECRET"]
PLAYBOOK = Path(__file__).resolve().parents[1] / "knowledge_support" / "playbook" / "approved.md"


def api(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        HOST + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + SECRET, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


content = PLAYBOOK.read_text(encoding="utf-8")
parts = re.split(r"(?m)^(###\s+.+)$", content)
entries = []
for i in range(1, len(parts), 2):
    heading = parts[i].strip().lstrip("#").strip()
    body = parts[i + 1].strip() if i + 1 < len(parts) else ""
    entries.append((heading, body))

existing = {e["heading"].lstrip("#").strip().lower() for e in api("GET", "/api/admin/support/knowledge/playbook")["entries"]}
print(f"host has {len(existing)} entries; repo has {len(entries)}")

for heading, body in entries:
    if heading.lower() in existing:
        print(f"skip (exists): {heading}")
        continue
    res = api("POST", "/api/admin/support/knowledge/playbook/entry", {"title": heading, "content": body})
    print(f"added: {heading} -> {res}")

final = api("GET", "/api/admin/support/knowledge/playbook")
print(f"final entry count: {final['entry_count']}")
sys.exit(0)
