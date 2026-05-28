# server/ — private API (secrets go here)

**Production:** Vercel runs this code via `api/index.py`.  
**Local:** Start with `..\1-START-LOCAL-API.ps1` (port 8780).

## Setup (first time)

1. Copy **`.env.example`** → **`.env`**
2. Fill in keys (see comments in `.env.example` and repo root **`GO-LIVE-VERCEL.md`**)
3. Never commit `.env` (gitignored)

## Main Python files

| File | What it does |
|------|----------------|
| **`app.py`** | All HTTP routes (`/api/realtime/session`, leads, Hammer Office, health) |
| **`lead_zapier.py`** | Sends signup to Zapier; verifies “I approve” callback |
| **`agreement_approvals.py`** | Stores approved emails (local JSON under `.data/`) |
| **`hammer_agreement.py`** | Builds agreement email HTML/text |
| **`hammer_office.py`** | Creates Hammer Office accounts (Playwright) |
| **`hammer_office_session.py`** | Live form fill during voice call |
| **`address_timezone.py`** | US/CA + timezone from address (no customer question) |
| **`sales_chat.py`** | Text chat fallback (if used) |
| **`wiki_retrieval.py`** | `search_wiki` tool — reads `wiki/` + `raw/hammer-data/` |

## Zapier email setup

See **`zapier/README.md`**

## Tests

Run from this folder: `py -3 -m pytest test_*.py -q`

## Scripts

| Path | Purpose |
|------|---------|
| **`scripts/test_create_account_tylers.py`** | One-off Hammer Office account test |
