# Zapier — Hammer Drive agreement email

The server builds the full agreement text on voice signup and sends it in the webhook as **`agreementEmailBody`**. You do not need to maintain the template inside Zapier unless you prefer HTML.

## Zap 1 filters

1. `event` equals `agreement_email_request`
2. `productLine` equals `hammer_drive`

## Gmail action (recommended)

| Field | Zapier field |
|--------|----------------|
| **To** | `email` |
| **Subject** | `agreementEmailSubject` |
| **Body (plain)** | `agreementEmailBody` |
| **Body (HTML + logo)** | `agreementEmailHtml` — Gmail **HTML** body (built-in red **HAMMER AI** banner; no broken image link) |

**Local dev (`http://127.0.0.1:5173` + API on `:8780`):** Use `agreementEmailHtml` in Gmail (HTML mode). The email includes a built-in red **HAMMER AI** HTML banner — Gmail cannot load `localhost` image URLs. Preview the PNG at `http://127.0.0.1:8780/email/hammer-ai-logo.png` in your browser. Optional: `HAMMER_EMAIL_LOGO_USE_IMAGE=1` only for local HTML preview, not real Gmail delivery.

**Dynamic dealership name:** never type a fixed name like "Victory Motorsports" in the Gmail step. Use **`agreementEmailBody`** (includes `Hello {their store},`) or **`emailGreetingLine`** / **`dealershipName`** if you build the body yourself. See `GMAIL_BODY_TEMPLATE.txt`.

## Optional structured fields (if you build the email in Zapier instead)

| Field | Example |
|--------|---------|
| `dealershipName` | Victory Motorsports |
| `subscriptionMonthlyDisplay` | $399 USD /month |
| `firstMonthBillingDisplay` | $399 USD/month (first month — no signup fee) |
| `billingSummary` | Month-to-month; no trial; no signup fee |
| `nextPaymentDate` | 6/17/26 |
| `serviceDescription` | HammerAI + Webchat |
| `lotBand` | 31–60 cars |

## Lot-size pricing (server-computed)

**United States (USD)** — pass `currency: USD` or omit.

| Vehicles on lot | Monthly |
|-----------------|--------|
| 11–30 | $299 |
| 31–60 | $399 |
| 61–80 | $599 |
| 81+ | $999 |

**Canada (CAD)** — pass `currency: CAD`, or include `CAD` / `Canada` in `selected_plan`, or a `.ca` website.

| Vehicles on lot | Monthly |
|-----------------|--------|
| 10–30 | $299 CAD |
| 31–60 | $399 CAD |
| 61–80 | $599 CAD |
| 81+ | $1,299 CAD |

**Billing:** Month-to-month only. **No signup fee, no activation fee, no trial.** First invoice = one month at the tier rate; next billing date ≈ 30 days after signup. Card is collected on first dashboard login — not a separate $5 charge.

## Dealership name

Use the **website** field from signup (dealership name or URL). The server sets `dealershipName` — e.g. `Victory Motorsports` or a title-cased domain.
