# Zapier — MarketPoster agreement email

Same Catch Hook as other products. Filter only on `event` = `agreement_email_request` (no `productLine` filter needed if using one Zap for all agreements).

## Gmail

| Field | Zapier field |
|--------|----------------|
| **To** | `email` |
| **Subject** | `agreementEmailSubject` |
| **Body (HTML)** | `agreementEmailHtml` |

## Seat-based pricing (server-computed)

| Users | Monthly (USD) |
|-------|----------------|
| 1 | $199 |
| 2 | $249 ($199 + $50) |
| 3 | $299 |
| 4 | $349 ($299 + $50) |
| 5 | $599 |
| 6+ | $599 + $50 per user above 5 |

Pass **`seat_count`** (e.g. `3 users`) or include users in **`selected_plan`** (e.g. `MarketPoster 3 users`).

Webhook fields: `seatCount`, `subscriptionMonthlyDisplay`, `additionalUserMonthlyDisplay`.
