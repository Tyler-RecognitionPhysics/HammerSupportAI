# Hammer Office account creation (voice PHASE B)

After the visitor replies **I approve** to the agreement email (Zap 2 → `/api/zapier/approval`), Tyler collects account fields and calls **`create_hammer_account`**. The server creates the dealership account at:

https://office.hammer-corp.com/accounts/new

## Server configuration

Add to `demo/realtime-sales-demo/server/.env`:

```env
HAMMER_OFFICE_EMAIL=your-staff@hammer-corp.com
HAMMER_OFFICE_PASSWORD=...
```

Optional:

- `HAMMER_OFFICE_DRY_RUN=1` — log in and map fields but do not submit (safe local test).
- `HAMMER_OFFICE_USE_PLAYWRIGHT=1` — use Chromium if the default httpx form POST does not match your Office build (`pip install playwright` then `playwright install chromium`).
- `HAMMER_OFFICE_HEADLESS=0` — **show a visible Chromium window** while creating accounts (watch each field fill).
- `HAMMER_OFFICE_SLOW_MO=400` — pause between actions (ms).
- `HAMMER_OFFICE_KEEP_OPEN=120` — leave the window open **120 seconds** after submit so you can review (seconds).
- `HAMMER_OFFICE_BASE_URL=https://office.hammer-corp.com` — override base URL.

The browser opens as a normal desktop window (taskbar). Cursor’s Simple Browser panel cannot host Playwright; use the popped Chromium window to watch automation.

The staff user must be allowed to create accounts in Hammer Office.

## Flow

1. PHASE A: `capture_lead` → agreement email → visitor replies **I approve**.
2. Zap 2 POSTs `/api/zapier/approval`.
3. PHASE B: `check_agreement_approval` → collect Hammer Office fields (one per turn).
4. `create_hammer_account` → `POST /api/hammer/create-account`.
5. Tyler delivers **PHASE C** one step per turn: confirm **Welcome to Hammer** email → **Activate** → password (**at least** ten characters; not "exactly ten") → **card on the next screen immediately after password** (never collect payment on the call; never all steps in one monologue) → **C.5 one turn:** after card, live rep will walk them through **their account** **and immediately ask** best callback time (same utterance — never announce rep then go silent); prefer **today**; duration (~5–10 min) only if they ask. Monthly billing / first 30 days start at dealership go-live, not at signup, activation, or card on file.

## PHASE B fields (voice)

| Field | Notes |
|-------|--------|
| email | From PHASE A — do not ask again |
| name | **Always ask first name, then last name** in PHASE B — never assume Tyler (Tyler is the rep only). Submit as First Last |
| legal_name | Legal business entity name |
| display_name | Public name (often same as dealership sign) |
| business_type | LLC, corporation, etc. |
| phone | Primary business phone |
| cell_phone | Mobile / cell |
| website | Business URL |
| address | Full street, city, state, zip — **timezone inferred from this** |
| currency | USD or CAD |
| dealership_name | From PHASE A |
| role | Owner, GM, etc. |
| gst_hst | Canadian, non-Quebec only |
| qst | Quebec only |
| selected_plan | From close |

**Not collected:** EIN / Tax ID, HubSpot URL, payment/card fields.

## Manual API test

```bash
curl -X POST http://127.0.0.1:8780/api/hammer/create-account \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"buyer@dealer.com\",\"name\":\"Jane Dealer\",\"legal_name\":\"Victory Motors LLC\",\"display_name\":\"Victory Motors\",\"business_type\":\"LLC\",\"phone\":\"5125550100\",\"cell_phone\":\"5125550101\",\"website\":\"victorymotors.com\",\"address\":\"123 Main St, Austin, TX 78701\",\"currency\":\"USD\",\"dealership_name\":\"Victory Motors\",\"role\":\"general-manager\",\"selected_plan\":\"Hammer Drive 31-60\"}"
```

Returns `403` if that email has not been approved via Zap 2.
