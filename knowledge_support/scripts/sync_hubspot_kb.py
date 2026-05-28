#!/usr/bin/env python3
"""CLI: sync HubSpot Knowledge Base articles into the support AI index."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2] / "demo" / "realtime-support-demo" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from hubspot_kb_sync import run_hubspot_kb_sync  # noqa: E402


def main() -> None:
    result = run_hubspot_kb_sync()
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
