"""
Vercel production entry — routes /api/* to the Hammer voice website backend.

Do not delete. The real app lives in:
  demo/realtime-sales-demo/server/app.py

Deploy config: vercel.json (repo root)
Go-live checklist: GO-LIVE-VERCEL.md (repo root)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "demo" / "realtime-sales-demo" / "server"
SHARED_DIR = ROOT / "demo" / "shared"

os.environ["REALTIME_SALES_REPO_ROOT"] = str(ROOT)
os.environ.setdefault("REALTIME_SALES_SERVERLESS", "1")
os.environ.setdefault("HAMMER_OFFICE_HEADLESS", "1")
os.environ.setdefault("HAMMER_LEARNING_ENABLED", "0")
os.environ.setdefault("REALTIME_SALES_PUBLIC_BASE_URL", "https://www.hammertime.com")

for entry in (str(ROOT), str(SERVER_DIR), str(SHARED_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Same-origin Vercel deploys: allow this deployment + local dev.
_cors = os.environ.get("REALTIME_SALES_CORS_ORIGINS", "").strip()
_extra: list[str] = []
for key in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
    host = os.environ.get(key, "").strip()
    if not host:
        continue
    origin = host if host.startswith("http") else f"https://{host}"
    _extra.append(origin.rstrip("/"))
# Custom domains (e.g. sellmeapen.vercel.app) may differ from VERCEL_URL.
for fixed in (
    "https://www.hammertime.com",
    "https://hammertime.com",
    "https://sellmeapen.vercel.app",
    "https://hammer-sell-me-a-pen-challenge.vercel.app",
):
    _extra.append(fixed)
if _extra:
    merged = [o.strip() for o in _cors.split(",") if o.strip()] + _extra
    os.environ["REALTIME_SALES_CORS_ORIGINS"] = ",".join(dict.fromkeys(merged))

from app import app  # noqa: E402

try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="auto")
except ImportError:  # pragma: no cover
    handler = app
