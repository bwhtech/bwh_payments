# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex and others) when working with code in this repository.

## What this is

`bwh_payments` is a Frappe app (Frappe 16+, Python 3.14) that takes payments through hosted checkout pages
on Stripe, Razorpay, Telr and Tabby, behind one gateway-agnostic API. ERPNext is optional. Developer docs:
https://docs.bwh.tech/bwh-payments/get-started/overview

The repo sits inside a bench at `/Users/harsh/bwh/bwh_payments` (`apps/bwh_payments`); the local site is
`bwhpayments.localhost`. Run `bench` commands from the bench root.

## Commands

```bash
# Tests (CI uses a site called test_site; allow_tests must be set once per site)
bench --site bwhpayments.localhost set-config allow_tests true
bench --site bwhpayments.localhost run-tests --app bwh_payments

# One module / one test
bench --site bwhpayments.localhost run-tests --module bwh_payments.tests.test_webhook
bench --site bwhpayments.localhost run-tests --module bwh_payments.tests.test_webhook --test test_a_correctly_signed_event_marks_the_request_paid
bench --site bwhpayments.localhost run-tests --doctype "Gateway Payment Request"

# Lint / format (same hooks CI runs; ruff, tabs, line length 110)
pre-commit run --all-files

# After editing a DocType JSON
bench --site bwhpayments.localhost migrate
```

CI (`.github/workflows/linter.yml`) also runs Frappe's semgrep rules and `pip-audit`.
PR titles must be conventional commits (`fix: ...`). Label a merged PR `backport <branch>` (e.g.
`backport develop`) to open the cherry-pick PR automatically.

## Architecture

### The gateway contract

`bwh_payments/base_class.py` defines `PaymentGatewayBase` (ABC): `create_session`, `get_payment_status`,
`refund_payment`, `handle_webhook`. Optional methods have defaults, so a third-party gateway that skips them
keeps working: `cancel_session` (False), `get_transaction_ref` (the settled payment's id; None),
`get_refund_status` (None = cannot report), `supported_currencies` (None = any), and the `logo` /
`logo_dark`, `description`, `docs_url` and `reusable_checkout` class attributes. `reusable_checkout` means
the checkout link stays payable after the shopper leaves it. `create_session` also takes an optional `return_url`, passed only when set.
Results are pydantic models: `CheckoutSession`, `RefundResult`, and `WebhookEvent | None` (a payment event
carries `session_id`, a refund event `refund_id`, `refund_amount` and `transaction_ref`). Callers read them through
`<Model>.model_validate`, so a third-party gateway that still returns dicts keeps working. Each gateway is a
**Single DocType** `<Name> Gateway Settings` whose controller inherits `(Document, PaymentGatewayBase)` and
holds the credentials (as `Password` fields, read with `get_password`).

A **Payment Gateway Profile** (named record, e.g. "Stripe") links to one settings Single via
`gateway_settings`. Its validate rejects any Single that does not implement the contract. Everything else
reaches a gateway only through `frappe.get_single(profile.gateway_settings)`, so a third-party app can add
a gateway without touching this app.

### Service clients (base / live / stub)

Every external service sits behind a client in `bwh_payments/services/<service>/`:

- `base.py`: an ABC of the calls the controller makes, plus models that mirror the service's own JSON. They
  subclass `bwh_payments.services.GatewayModel`, which ignores unknown fields and reads bare numbers into
  `str` fields. They keep only the fields we read, and are optional wherever the service sends null.
- `live.py`: the transport. It makes the HTTP or SDK call, logs it with `create_request_log` (endpoint and
  ids only, never payloads), turns a refusal into `frappe.ValidationError`, and parses the response into the
  model. It holds no business decisions. Live clients are not tested, so anything that branches on payment
  state belongs in the controller.
- `stub.py`: an in-memory version of the service with the same methods. State lives in `ClassVar`s.
  Tests drive outcomes through classmethod controls (`pay`, `authorise`, `set_status`, `add_*`,
  `next_refund_status`) and call `reset()` in `setUp`. Recordings (`created_*`, `refunds`) keep the
  service's own request shapes. Webhook signing and event builders for tests live here too.

The settings controller picks the client in `get_client()`: the stub when `frappe.in_test`, otherwise the
live one. There is no other switch, so a real site can never run on a stub. The controller keeps every
decision: currency conversion, URL building, status maps, capture and refund rules, and webhook signature
checks. Signature checks are local crypto and must never move into a client a stub could replace.

Live HTTP clients pass no timeout: `make_request` accepts one only from Frappe 17. Add it when Frappe 16
support is dropped.

To add a service, copy `services/razorpay/` and wire `get_client()` the same way.

### Public API for other apps

`bwh_payments/api.py` is the supported import surface. None of it checks permissions, so never whitelist it
as it stands.

- `create_payment(ref_doctype, ref_docname, amount, currency, gateway, **fields)`: `fields` limited to
  `company`, `redirect_to` (a path on this site) and `customer_*`.
- `get_payment_gateways` (names), `get_gateway_options(currency=None)` (`GatewayOption`: name, title,
  logos), `validate_currency(gateway, currency)`, `resolve_payment_mode`.
- `get_gateways()` (`GatewayInfo`): every installed gateway, enabled or not, with its settings DocType,
  description, docs link, `reusable_checkout` and logos, so a consumer's settings screen needs no gateway list.
- `refund(payment_request, amount=None, reference=None)` returns the Gateway Refund;
  `sync_refunds(payment_request)`.
- `record_external_payment` / `record_external_refund`: import history settled elsewhere, idempotent,
  no callbacks.
- `to_minor_units`, `from_minor_units`.

Callbacks on the referenced document: when a request turns `Paid`, `on_update` calls
`run_method("on_payment_authorized", "Completed")` once (the frappe/payments convention). Every Gateway
Refund status change calls `run_method("on_refund_update", refund)`; `refund.reference` is the caller's id.
An insert that is already settled is an import and fires neither.

`bwh_payments/testing.py` lets consumer apps test against the in-memory gateway without naming a provider:
`configure_test_gateway`, `pay`, `set_next_refund_status`, `set_refund_status`, `send_refund_webhook`,
`send_external_refund_webhook`, `without_commits`. `bwh_payments/migrate/frappe_payments.py`
`import_gateways()` turns frappe/payments gateways into profiles with the same names.

### Payment flow

1. Caller inserts a **Gateway Payment Request** (`ref_doctype`/`ref_docname`, `gateway` = profile name,
   `amount`, `currency_code`, optional customer fields and `redirect_to`). A new checkout must start
   `Pending`; `amount`, `currency_code`, `gateway` and `order_ref` are `set_only_once`.
2. `before_save` calls `create_session` once (when `order_ref` is empty) and stores `order_ref` (gateway
   session id, unique), `order_url` (checkout URL for the shopper) and the return URLs. With `redirect_to`,
   every return URL is `checkout.return_from_gateway?request=<name>`.
3. Status moves from `Pending` by one of three paths, all under a row lock (`lock_row`). On the move to
   `Paid`, `gateway_transaction_ref` is read through `get_transaction_ref` (a failure never blocks it).
   - **Webhook**: `webhook.handle` (guest POST, `?gateway=<profile>`, rate-limited) verifies the delivery,
     logs the parsed event as a Queued Integration Request (deduped on the event id), answers 200 and
     queues `process_webhook`. The job applies it as Administrator; a failure is rolled back to a
     savepoint, logged and marked Failed for `retry_webhook` (System Manager). Deadlocks and lock timeouts
     are raised as `RetryBackgroundJobError`, because Frappe's job runner does not retry the
     `QueryDeadlockError` / `QueryTimeoutError` it wraps them in.
   - **Return**: `checkout.return_from_gateway` (guest GET, rate-limited) runs `sync_status` and redirects
     to `redirect_to`. Never trust the browser redirect itself.
   - **Pull**: whitelisted `sync_status` asks the gateway.
4. `create_refund(amount=None, reference=None, payment_entry=None)` checks the amount under the row lock,
   inserts a **Gateway Refund** as `Pending` and commits it before calling the gateway, so the reservation
   holds once the lock is gone (Pending refunds count against what is left). The gateway's answer is
   normalised to `Pending` / `Processed` / `Failed`; a refusal is kept as `Failed` with its error. Only a
   `Processed` refund adds to `refund_amount` and sets `Partially Refunded` / `Refunded`. Pending refunds
   settle by refund webhook or `sync_refunds`; a refund raised on the gateway's dashboard arrives by
   webhook and is found through `gateway_transaction_ref`. `refund()` and `release_if_unpaid()` are not
   whitelisted: Frappe's `run_doc_method` checks only read permission.
5. ERPNext bridge (`hooks.py` `doc_events`): submitting a `Payment Entry` with `payment_type = "Pay"`,
   `reference_no` = `order_ref` and `mode_of_payment` = the gateway profile name refunds through the
   gateway. Amended entries are traced back via `amended_from` so a refund is never sent twice.

### Gateway-specific behavior worth knowing

- **Stripe**: official SDK through a `stripe.StripeClient` per `LiveStripeClient` (never module-level
  `stripe.api_key`). SDK objects are attribute-based (no `.get()`), so the live client parses with `from_attributes=True`.
  Refund events: `refund.created`, `refund.updated`, `refund.failed`, `charge.refund.updated`.
  `stripe>=12.5` cannot share a venv cleanly with frappe/payments' `stripe~=10.12` pin.
- **Razorpay**: raw HTTP (Payment Links API) via `frappe.integrations.utils.make_post_request` /
  `make_get_request`, no SDK. Refund events: `refund.created` (pending), `refund.processed`,
  `refund.failed`. A session id starting `pay_` is a payment imported from before Payment Links and is
  refunded directly.
- **Telr**: no signed webhook. `handle_webhook` returns `None` on purpose; status comes only from
  `get_payment_status`.
- **Tabby**: unsigned webhooks, checked with a static header token plus a source-IP allowlist.
  `get_payment_status` also *captures* an authorised payment; that capture is where money is taken.

### Money and currency rules

- Amounts crossing `PaymentGatewayBase` are always in **major units**. Convert with
  `bwh_payments/currency.py` (`to_minor_units` / `from_minor_units`), which uses the pinned
  `MINOR_UNIT_EXPONENTS` table (ISO 4217). Do not read precision from the Currency DocType's
  `fraction_units`; it is wrong for currencies such as JPY.
- Refund arithmetic rounds to the currency's minor-unit exponent, not the field precision.
- `GatewayPaymentRequest.validate_amount_precision` refuses amounts the site cannot store exactly (for
  example KWD on a site without "Use Number Format From Currency").

### Security conventions in `webhook.py`

Every verification failure returns the same opaque 400 (`reject()`), and `message_log` is cleared so a
`frappe.throw` from a verifier does not leak. A verified delivery that `WebhookEvent` cannot validate is a
400 too, so the gateway retries it and it lands in the Error Log. Do not log webhook payloads; they can contain cardholder
data. Every call is recorded as an Integration Request via `create_request_log`, holding only the parsed
`WebhookEvent` (ids, status, amounts).

`redirect_to` must be a path on this site (`is_local_path` refuses absolute URLs, `//host` and `/\host`),
because `return_from_gateway` is a guest endpoint that redirects to it.

## Tests

Tests are `IntegrationTestCase` and never touch the network. Controllers pick their stub client under
tests on their own, so tests do not patch transports: call `Stub<Service>Client.reset()` in `setUp`, drive
the outcome with its controls, and assert on its recordings. The real controller code runs every time.

Frappe 17's `IntegrationTestCase` rolls back only at class end, and `create_request_log` (webhook logging)
and `create_refund` (the reservation) commit. That is why the Stripe and Razorpay fixtures come with
`remove_*_gateway` helpers that run in `tearDownClass`, and why consumer apps wrap their test classes in
`testing.without_commits()`. Stub event ids are unique by default: webhook logs dedupe redeliveries.

Reuse the fixtures `configure_stripe_gateway` and `make_payment_request` (in
`test_gateway_payment_request.py`) and `configure_razorpay_gateway` (in `test_razorpay_gateway_settings.py`).
Tests inside a doctype folder set `IGNORE_TEST_RECORD_DEPENDENCIES` to avoid pulling in ERPNext test
records. Do not set it in `bwh_payments/tests/`: Frappe raises `NotImplementedError` outside a doctype
folder, and loads no test records there anyway. Webhook tests
build a werkzeug `Request` into `frappe.local.request` and call `webhook.handle()` directly.

## Conventions

- `hooks.py` sets `require_type_annotated_api_methods = True`: every `@frappe.whitelist` method needs
  type annotations. `export_python_type_annotations = True` regenerates the `# begin: auto-generated types`
  block in controllers on migrate; do not edit that block by hand.
- `use_json_request_body = True`: non-GET calls to this app's endpoints send JSON bodies.
- Keep comments rare: only for a non-obvious reason (security, money, a gateway quirk). Do not add
  `# ponytail:` or similar marker comments.
- Name variables and functions with simple technical terms that say what they do (`lock_row`,
  `get_refund_summary`), not jargon or metaphor. Do not rename whitelisted methods, `api.py` functions or
  stub controls without checking callers in other apps.
- `get_available_payment_modes` uses `redis_cache` (1 h TTL), cleared on profile update, trash and rename,
  and again after commit or rollback.
