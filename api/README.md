# api/ — Vercel server entry (do not delete)

**`index.py`** is the only file Vercel needs here. It loads the FastAPI app from:

`demo/realtime-sales-demo/server/app.py`

All `/api/*` routes in production go through this file (see root `vercel.json` rewrites).

You normally **do not edit** this folder unless you change how the backend is deployed.
