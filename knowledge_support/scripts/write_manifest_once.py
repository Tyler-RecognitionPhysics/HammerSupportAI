#!/usr/bin/env python3
"""One-shot manifest writer for Fly seed/deploy ops."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge_support.kb_artifact import write_manifest  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(write_manifest(), indent=2))
