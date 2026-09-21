<div align="center" markdown="1">

<img src="bwh_payments/public/images/bwh_payments.svg" alt="BWH Payments logo" width="80" />
<h1>BWH Payments</h1>

<a href="https://buildwithhussain.com"><img src=".github/built-at-bwh.svg" alt="Built at BWH" height="28" /></a>

**Hosted checkout for Frappe and ERPNext — one contract, every gateway**

<p>
	<img src=".github/logos/stripe.svg" alt="Stripe" height="40" />
	<img src=".github/logos/razorpay.svg" alt="Razorpay" height="40" />
	<img src=".github/logos/telr.svg" alt="Telr" height="40" />
	<img src=".github/logos/tabby.svg" alt="Tabby" height="40" />
</p>

</div>

## BWH Payments

Your app hands BWH Payments an amount and a gateway name, and gets back a hosted checkout URL. What
happens after that — the redirect, the webhook, the replay, the refund — is the same story whichever
gateway is behind it. A `Gateway Payment Request` records the gateway's side of the conversation, and your
app decides what a paid request turns into.

📖 **[Developer docs](https://bwhdocs.fsn.frappe.cloud/bwh-payments/get-started/overview)**: take a
payment, build return pages, add your own gateway, and set up each built-in one.

### Gateways

- **Stripe** — Cards and wallets, worldwide. Checkout Sessions with verified webhook signatures.
- **Razorpay** — Cards, UPI, netbanking and wallets across India, on Payment Links.
- **Telr** — Cards and local methods across the GCC, including three-decimal currencies.
- **Tabby** — Buy now, pay later in four instalments across MENA.

### Key Features

- **One session record, not a second ledger.** `Gateway Payment Request` holds the gateway session id,
  the hosted checkout URL, the payment status and the refund ledger. `order_ref` is unique, so an order
  can never grow a second live session.

- **Replay-safe by construction.** Every webhook is signature-verified and returns the gateway's own event
  id, so a retried delivery racing a shopper's return is dropped rather than billed twice. Bad signature,
  unknown gateway and missing gateway all answer the same opaque error — nobody can enumerate what a site
  has configured.

- **Refunds, full or partial.** Booked against the session. With ERPNext installed, submitting a refund
  Payment Entry sends the refund to the gateway too.

- **Money that survives the round trip.** Amounts cross the provider boundary in major units and convert
  once, centrally — never a hardcoded `* 100`. A three-decimal currency (KWD, BHD, OMR) whose site would
  silently round 12.345 to 12.35 has its charge refused rather than billed at a different figure.

- **BNPL that can say no.** Tabby may decline a shopper outright, so the checkout offers another method
  instead of showing an error, and an authorised payment is captured before it is ever reported Paid.

- **One switch per gateway.** Each gateway keeps its credentials in its own settings Single and is turned
  on independently with a `Payment Gateway Profile`.

### Installation

BWH Payments runs on Frappe 16 or later. ERPNext is optional.

```bash
bench get-app https://github.com/bwhtech/bwh_payments
bench --site your.site install-app bwh_payments
```

Then fill in a gateway's settings and create a `Payment Gateway Profile` for it. Each gateway's setup is
in the [docs](https://bwhdocs.fsn.frappe.cloud/bwh-payments/gateways/stripe).

### Adding a gateway

Subclass `PaymentGatewayBase` on a Single DocType in your own app, and implement four methods:
`create_session`, `get_payment_status`, `refund_payment` and `handle_webhook`. Nothing in checkout, the
webhook endpoint or the refund path needs to know the new name. The
[step-by-step guide](https://bwhdocs.fsn.frappe.cloud/bwh-payments/build/build-a-payment-gateway) builds
one from scratch, tests included.

### Under the Hood

- [Frappe Framework](https://github.com/frappe/frappe) — Full-stack Python web framework.
- [ERPNext](https://github.com/frappe/erpnext) — Optional. Refund Payment Entries reach the gateway.

## About BWH Studios

BWH Payments is developed and maintained by BWH Studios, a tech company based in Jagdalpur, Chhattisgarh,
specializing in Frappe customizations and consulting.

#### License

MIT
