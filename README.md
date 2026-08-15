## BWH Payments

Hosted-checkout payment gateway integrations for Frappe/ERPNext. Ships Stripe and Telr, and a contract
any further gateway can implement.

### What it is

`Gateway Payment Request` is a **gateway session record**, not a replacement for ERPNext's Payment
Request. It records the gateway session id, the hosted checkout URL, the payment status and the refund
ledger. All GL movement stays in ERPNext (Sales Order → Sales Invoice → Payment Entry); the consumer app
owns that half.

| DocType | Purpose |
|---|---|
| `Payment Gateway Profile` | Registry row: which settings Single backs which gateway, and whether it is enabled. Keeps the core `Payment Gateway` row in sync so `payments.utils.get_payment_gateway_controller` resolves. |
| `Gateway Payment Request` | One shopper payment: session id, status, refund ledger. Not submittable. `order_ref` is unique. |
| `Stripe Gateway Settings` | Stripe credentials and redirect URLs. |
| `Telr Gateway Settings` | Telr credentials and return URLs. |

### Setup

1. `bench get-app https://github.com/Rl0007/bwh_payments && bench --site <site> install-app bwh_payments`
2. Fill in `Stripe Gateway Settings` (or `Telr Gateway Settings`). The Stripe webhook secret is
   mandatory — an unverified webhook is an "anyone can mark an order paid" hole.
3. Create a `Payment Gateway Profile` named after the gateway, pointing at that settings DocType, and
   enable it. Its name is what the storefront sends and what the matching `Mode of Payment` must be
   called.
4. Point the gateway's webhook at
   `POST /api/method/bwh_payments.bwh_payments.webhook.handle?gateway=<Payment Gateway Profile name>`

### Three-decimal currencies (KWD, BHD, OMR)

Frappe derives a Currency field's precision from the **site's** default number format unless System
Settings has **Use Number Format From Currency** enabled. Without it a 12.345 KWD charge is stored as
12.35 and the shopper is billed a different figure, so `Gateway Payment Request` refuses the charge
rather than round it. Turn that setting on before taking payments in a 3-decimal currency.

### Adding a gateway

Subclass `bwh_payments.base_class.PaymentGatewayBase` on a Single DocType and implement
`create_session`, `get_payment_status`, `refund_payment` and `handle_webhook`. Amounts crossing that
boundary are in **major** units; convert with `bwh_payments.currency.to_minor_units`, never a hardcoded
`* 100`. `handle_webhook` must verify the gateway's signature and return the gateway's event id so
replays can be dropped.

### Dependencies

No `stripe` pin is declared here on purpose: `frappe/payments` pins `stripe~=10.12.0` in the same bench
venv and every API used (`StripeClient`, `checkout.sessions.create/retrieve`, `refunds.create`,
`Webhook.construct_event`) exists there.

### Tests

```bash
bench --site <site> run-tests --app bwh_payments
```

They run against a fake Stripe transport (`bwh_payments/tests/fake_stripe.py`) with real signature
verification — no live gateway calls, ever.

#### License

mit
