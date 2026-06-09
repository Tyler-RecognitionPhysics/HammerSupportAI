#!/usr/bin/env python3
"""CLI: sync HubSpot resolved tickets and rebuild support_kb.sqlite."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "demo" / "realtime-support-demo" / "server"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync HubSpot resolved tickets into the support KB.")
    parser.add_argument("--full-backfill", action="store_true", help="Ignore last sync and fetch all tickets.")
    args = parser.parse_args()

    from hubspot_tickets_sync import run_hubspot_tickets_sync_async

    result = asyncio.run(run_hubspot_tickets_sync_async(full_backfill=args.full_backfill))
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
