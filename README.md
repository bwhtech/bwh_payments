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

A storefront hands BWH Payments an order and a gateway name, and gets back a hosted checkout URL. What
happens after that — the redirect, the webhook, the replay, the refund — is the same story whichever
gateway is behind it. ERPNext stays the ledger: Sales Order → Sales Invoice → Payment Entry is untouched,
and a `Gateway Payment Request` records only the gateway's side of the conversation.

It is the payments half of [**Commera**](https://github.com/bwhtech/commera), and its shipping sibling is
[**bwh_shipping**](https://github.com/bwhtech/bwh_shipping).

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

- **Refunds, full or partial.** Booked against the session and reconciled back into ERPNext.

- **Money that survives the round trip.** Amounts cross the provider boundary in major units and convert
  once, centrally — never a hardcoded `* 100`. A three-decimal currency (KWD, BHD, OMR) whose site would
  silently round 12.345 to 12.35 has its charge refused rather than billed at a different figure.

- **BNPL that can say no.** Tabby may decline a shopper outright, so the checkout offers another method
  instead of showing an error, and an authorised payment is captured before it is ever reported Paid.

- **Configured from the dashboard.** Each gateway keeps its own credentials in its own settings Single and
  is switched on independently, through Commera's integrations screen rather than the desk.

### Adding a gateway

Subclass `PaymentGatewayBase` on a Single DocType and implement four methods — `create_session`,
`get_payment_status`, `refund_payment` and `handle_webhook`. Nothing in checkout, the callback route or
the refund path needs to know the new name.

### Under the Hood

- [Frappe Framework](https://github.com/frappe/frappe) — Full-stack Python web framework.
- [ERPNext](https://github.com/frappe/erpnext) — The accounting the gateways never duplicate.

## About BWH Studios

BWH Payments is developed and maintained by BWH Studios, a tech company based in Jagdalpur, Chhattisgarh,
specializing in Frappe customizations and consulting.

#### License

MIT
