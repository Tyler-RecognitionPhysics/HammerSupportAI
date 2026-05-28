"""
Vercel production entry for Hammer Support AI.

Routes /api/* and /admin/support to the support demo backend.
Use vercel-support.json as the Vercel project config (separate deployment from sales site).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "demo" / "realtime-support-demo" / "server"
SHARED_DIR = ROOT / "demo" / "shared"

os.environ["SUPPORT_REPO_ROOT"] = str(ROOT)
os.environ.setdefault("SUPPORT_SERVERLESS", "1")

for entry in (str(ROOT), str(SERVER_DIR), str(SHARED_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_cors = os.environ.get("SUPPORT_CORS_ORIGINS", "").strip()
_extra: list[str] = []
for key in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
    host = os.environ.get(key, "").strip()
    if not host:
        continue
    origin = host if host.startswith("http") else f"https://{host}"
    _extra.append(origin.rstrip("/"))
if _extra:
    merged = [o.strip() for o in _cors.split(",") if o.strip()] + _extra
    os.environ["SUPPORT_CORS_ORIGINS"] = ",".join(dict.fromkeys(merged))

from app import app  # noqa: E402

try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="auto")
except ImportError:  # pragma: no cover
    handler = app
