---
name: add-payment-gateway
description: Add a new payment provider (e.g. PayPal, Paymob, Checkout.com, Tap, HyperPay, Moyasar, Adyen) to bwh_payments as a Gateway Settings Single plus a base/live/stub service client in bwh_payments/services/NAME/, wired into the gateway contract, webhooks, refunds, tests and the storefront admin. Use this whenever someone wants to support, integrate, add, or "plug in" a new payment gateway, PSP, BNPL or hosted checkout in bwh_payments or commera, even if they only name the provider.
---

# Add a payment provider to bwh_payments

A provider is three things: a **Single DocType** that holds credentials and implements
`PaymentGatewayBase`, a **service client** (`services/<name>/{base,live,stub}.py`) that talks to the
provider, and a **Payment Gateway Profile** record that makes it selectable. Read `AGENTS.md`
("The gateway contract", "Service clients", "Money and currency rules", "Security conventions") before
starting. Those rules are not repeated here. Copy structure from the closest existing gateway:

| Provider looks like | Copy from |
|---|---|
| raw JSON HTTP API, HMAC-signed webhooks | `razorpay` |
| official Python SDK | `stripe` |
| no signed webhook, status only by polling | `telr` |
| authorise-then-capture (BNPL), token-checked webhooks | `tabby` |

Branch in every repo you will touch, and make sure the bench's Redis is running before you run tests
(see the migrate skill, step 0).

## 1. Pin down the provider's facts first

Get these from the provider's API docs (ask the user for the link if you don't have one) and write them
in your first message back. Each one changes code, and guessing any of them costs real money:

- **Checkout:** which endpoint creates a hosted page, which id identifies it for later status reads, and
  whether that id is the one webhooks carry. Tabby's checkout id is not; its payment id is.
- **Amount format:** integer minor units (Stripe, Razorpay), a major-unit decimal string at the
  currency's precision (Tabby, Telr), or something else. Find out how 3-decimal (KWD, BHD) and 0-decimal
  (JPY) currencies are handled.
- **Supported currencies and markets.**
- **Status vocabulary,** and which statuses mean money is actually taken.
- **Capture model:** automatic, manual, or per-account. If it can't be known from the API, add a
  `treat_authorised_as_paid` style switch that defaults to the safe side, as Razorpay and Telr do.
- **Webhook verification:** an HMAC over the raw body (with which secret), a static token, IP allowlists,
  or nothing. Also find which header carries an event id; derive a stable one if there is none, as Tabby
  does.
- **Refunds:** what you refund against (session, payment, transaction), whether partial refunds are
  allowed, what an accepted refund looks like, and whether the refund id comes back.
- **Errors:** the error body shape, and whether failures come back as HTTP 200 (Telr does).

## 2. Create the settings Single

Do not `mkdir` doctype folders. With `developer_mode` on, create the DocType through the desk or
`bench --site <site> console` (module `BWH Payments`, `issingle: 1`, System Manager permissions), then
`bench --site <site> migrate`. Mirror the Razorpay field layout:

- `enabled`, `test_mode`, plus any capture or notification switches (Check)
- credentials: public ids as Data, secrets as **Password** (required where the API can't work without them)
- `webhook_secret` (Password) and, if the provider publishes IPs, `webhook_ips` (Small Text)
- `currency` (Link), `success_url`, `failure_url`, `cancelled_url` (Data)

Migrate regenerates the `# begin: auto-generated types` block in the controller. Don't write it by hand.

## 3. Build the service client

Follow AGENTS.md "Service clients" and the migrate skill steps 3–5 for `base.py`, `live.py` and `stub.py`.
For a new provider:

- `base.py` methods map one to one to the provider calls you listed in step 1. Model only the fields the
  controller reads.
- `stub.py` must behave like the provider's real state machine for the paths you support: created, then
  paid or authorised, then captured, then refunded. Include the refusal paths (`next_refund_status`, a
  rejection control) and raise for unknown ids the way live does.

## 4. Write the controller

`class <Name>GatewaySettings(Document, PaymentGatewayBase)` with `get_gateway_name()` and `get_client()`
(stub iff `frappe.in_test`). Then implement the contract:

- **`create_session(amount, currency, reference, customer)` → `CheckoutSession`.**
  - Validate the currency with `validate_transaction_currency`, plus the provider's own allowlist if it
    has one.
  - Convert amounts only through `bwh_payments/currency.py`.
  - Build return URLs with `urlsplit` and `parse_qsl`, append `reference_id=<request name>`, and run them
    through `get_localised_url`.
  - Drop customer fields the provider rejects when blank, rather than letting checkout fail.
  - `session_id` must be the id webhooks and status reads use.
- **`get_payment_status(session_id)` → a Gateway Payment Request status.**
  - Normalise once (`strip().casefold()`), then use a status map in which anything unrecognised is
    `Pending`.
  - A partial or authorised-but-uncaptured payment is never `Paid` unless the store's switch says so. If
    the provider needs an explicit capture, do it here, for the authorised amount only (see Tabby).
- **`handle_webhook(payload, headers)` → `WebhookEvent | None`.**
  - Verify first: HMAC over the raw bytes with `hmac.compare_digest`, guard non-ASCII signatures with
    `isascii()`, and `frappe.throw` on a missing secret, header or event id.
  - Return `None` for events you don't act on.
  - Throw on a "paid" event you can't read, so the provider retries and it lands in the Error Log.
  - Header names must match werkzeug's title-case (`X-Foo-Signature`), because the handler receives a
    plain dict.
- **`refund_payment(session_id, amount, currency)` → `RefundResult`** with `amount` in major units. The
  request reads only `refund_id`, and it reads it after the money has moved, so return what you have
  rather than raising on a thin provider response.
  Refuse when there is nothing captured to refund, and throw on any status that isn't an accepted one.
- **`cancel_session(session_id)` → bool,** if the provider can cancel or expire a session. Return
  `False` instead of raising when the provider refuses.

Mark deliberate limits with `# ponytail:` comments that name the ceiling and the upgrade path (a missing
void endpoint, no timeout on `make_request`, and so on).

## 5. Tests (stub-driven, in the doctype folder)

`test_<name>_gateway_settings.py` with `StubXClient.reset()` in `setUp`. Cover at least:

- amounts for a 2-, a 3- and a 0-decimal currency, as sent to the provider
- an unsupported currency refused before any call
- the session id being the one webhooks use, and the return URLs carrying `reference_id`
- the status map: each known status, an unknown one staying `Pending`, and authorised versus captured
- capture, if any: triggered once, for the authorised amount
- webhooks: valid, forged, missing header, wrong secret (for example the API key in place of the webhook
  secret), an ignored event type, and an unreadable paid event
- refunds: accepted, refused, nothing captured, unknown session
- `release_if_unpaid` on an open session and on a paid one
- one spine test through `bwh_payments.bwh_payments.webhook.handle` (see `tests/test_razorpay_webhook.py`)

If the class writes Integration Requests or Profiles, clean up in `tearDownClass` (see
`remove_razorpay_gateway`), because Frappe 17 rolls back only at class end and `create_request_log`
commits.

## 6. Make it selectable

- **Payment Gateway Profile:** a record named after the provider, with `gateway_settings` set to the new
  Single. Its validate rejects a Single that doesn't implement the contract.
- **ERPNext refunds** need a Mode of Payment named exactly like the profile.
- **Commera:** add an entry to `PAYMENT_GATEWAYS` in `commera/api/admin/payments.py` (slug, label, blurb,
  `settings_doctype`, docs URL). Enabling it there creates the Mode of Payment, and the webhook URL shown
  to the merchant is `/api/method/bwh_payments.bwh_payments.webhook.handle?gateway=<profile>`.
- For an end-to-end guarantee in commera, copy `commera/tests/test_razorpay_checkout.py` and swap the
  driver helpers for the new stub.

## 7. Document and verify

- Add a "Gateway-specific behavior" bullet to AGENTS.md: transport, webhook scheme, and anything
  surprising.
- Run the full bwh_payments and consumer suites, `pre-commit run --files ...`, and a leak check (no
  stray profiles or enabled settings on the site).
- One commit in bwh_payments (`feat: add <Name> gateway`), and a separate one for the commera wiring.
