# Zapier — Facebook AIA agreement email

Same flow as Hammer Drive: voice `capture_lead` → `POST /api/lead` → Catch Hook → Gmail.

## Zap filters

1. `event` equals `agreement_email_request`
2. `productLine` equals `facebook_aia`

(Or use one Zap with a filter group: `productLine` is `hammer_drive` OR `facebook_aia`.)

## Gmail action

| Field | Zapier field |
|--------|----------------|
| **To** | `email` |
| **Subject** | `agreementEmailSubject` (e.g. `Facebook AIA agreement — Sunrise Ford`) |
| **Body (HTML)** | `agreementEmailHtml` — Gmail **HTML** mode |

## Facebook AIA fields in webhook

| Field | Example |
|--------|---------|
| `productLine` | `facebook_aia` |
| `agreementTemplate` | `facebook_aia` |
| `subscriptionMonthlyDisplay` | `$299 USD/month` |
| `metaAdSpendDailyDisplay` | `$15/day` |
| Service line in email | Facebook Advertising + AI |
| `firstMonthBillingDisplay` | `$299 USD today` |
| `nextPaymentDate` | `6/17/26` |
| `serviceDescription` | Facebook AIA — Meta inventory ads on Facebook & Instagram… |

## Pricing (server-computed)

| Item | Amount |
|------|--------|
| Hammer fee | **$299/month** (month-to-month) — **flat for every lot size** (not Drive-tiered) |
| Meta ad spend minimum | **$15/day** in addition to the $299 (full inventory; billed by Meta, separate from Hammer fee) |

Voice agent should pass `selected_plan` containing **Facebook AIA** (e.g. `Facebook AIA`, `Facebook AIA $299/mo`).
