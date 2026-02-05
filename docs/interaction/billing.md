# Interaction Billing Facts

## Default Model
- The site can stay live without a hire unless billing status says payment is required.
- Hiring Demi is a monthly retainer to keep work moving.
- Backend add-ons are optional and billed separately.
- If no price is available, say the exact monthly cost will be confirmed before any charge.

## Source of Truth
- If tasks/billing_status.json exists, use it.
- Never invent prices.
- Managed backend add-on pricing comes from docs/backend_pricing.md.

## When Payment Is Required
- If payment_required=true and allow_first_build=false, ask to hire and send the payment link.
- If payment_required=true and allow_first_build=true, deliver first build, then ask to hire.
- If payment_required=true and there is no order_id/payment_url, request a subscription first.

## Error Cases
- If billing is unconfigured or pricing is missing, say pricing is not configured yet and you will confirm before any charge.
- If the user says they paid but billing still shows unpaid, say you are waiting on bank confirmation and will resume automatically.
