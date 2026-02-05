# Billing Guide (Internal)

Purpose
- Provide consistent, non-hallucinated answers to pricing and hiring questions.
- Use the billing status payload as the source of truth when it exists.

Source of Truth
- If `tasks/billing_status.json` exists, use it.
- Do not invent prices. Only quote `price_usd`/`currency` from billing status or explicit quotes.
- For managed backend add-on pricing, use `docs/backend_pricing.md`.

Default Model (when no payment is required)
- The current site can stay live without a hire unless billing status says payment is required.
- Hiring Demi is a monthly retainer to keep work moving.
- Backend add-ons are optional and billed separately.
- If no price is available, say you will confirm the exact monthly cost before any charge.

Managed Backend Add-on Pricing
- Managed backend is optional and billed separately from the assistant subscription.
- Use these tiers when answering pricing questions. Do not invent prices.
- Paid plan constraint: new instances cannot be Nano. Treat Nano as Micro pricing on paid plans.

| Tier | Hourly Price (USD) | Monthly Price (USD) |
| --- | --- | --- |
| Nano | 0 | 0 |
| Micro | 0.01613 | ~12 |
| Small | 0.02472 | ~18 |
| Medium | 0.09864 | ~72 |
| Large | 0.18204 | ~132 |
| XL | 0.34524 | ~252 |
| 2XL | 0.6744 | ~492 |
| 4XL | 1.584 | ~1,152 |
| 8XL | 3.0744 | ~2,244 |
| 12XL | 4.6032 | ~3,360 |
| 16XL | 6.144 | ~4,476 |

When Payment Is Required
- If `payment_required=true` and `allow_first_build=false`, ask them to hire and send the payment link.
- If `payment_required=true` and `allow_first_build=true`, deliver the first build, then ask to hire.
- If `payment_required=true` and there is no `order_id`/`payment_url`, call `request_assistant_subscription`.
- If `message` is `usage_threshold_exceeded`, say the usage cap was reached and a hire is needed to continue.

Error / Unconfigured Cases
- If status is `unconfigured` or message is `assistant_pricing_missing`, say pricing is not configured yet and you will confirm before any charge.
- If the user says they paid but status is still unpaid, say you are waiting for the bank to confirm and you will resume automatically.

Common Billing Status Fields
- `status`, `payment_required`, `allow_first_build`, `plan`, `order_id`, `payment_url`
- `price_usd`, `currency`
- `usage_total_usd`, `usage_threshold_usd`
- `purpose`, `purpose_label`
