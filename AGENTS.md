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

## Architecture

### The gateway contract

`bwh_payments/base_class.py` defines `PaymentGatewayBase` (ABC): `create_session`, `get_payment_status`,
`refund_payment`, `handle_webhook`, plus optional `cancel_session`. Results are pydantic models:
`CheckoutSession`, `RefundResult`, and `WebhookEvent | None`. Callers read them through
`<Model>.model_validate`, so a third-party gateway that still returns dicts keeps working. Each gateway is a
**Single DocType** `<Name> Gateway Settings` whose controller inherits `(Document, PaymentGatewayBase)` and
holds the credentials (as `Password` fields, read with `get_password`).

A **Payment Gateway Profile** (named record, e.g. "Stripe") links to one settings Single via
`gateway_settings`. Its validate rejects any Single that does not implement the contract. Everything else
reaches a gateway only through `frappe.get_single(profile.gateway_settings)`, so a third-party app can add
a gateway without touching this app.

### Service clients (base / live / stub)

Every external service sits behind a client in `bwh_payments/services/<service>/`:

- `base.py`: an ABC of the calls the controller makes, plus pydantic models that mirror the service's own
  JSON (only the fields we read, `extra="ignore"`, optional where the service sends null).
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

To add a service, copy `services/razorpay/` and wire `get_client()` the same way.

### Public API for other apps

`bwh_payments/api.py` is the supported import surface: `create_payment(ref_doctype, ref_docname, amount,
currency, gateway, **fields)`, `get_payment_gateways`, `resolve_payment_mode`, `to_minor_units`,
`from_minor_units`. It skips permission checks, so never whitelist it as it stands. When a request turns
`Paid`, `on_update` calls `ref_doc.run_method("on_payment_authorized", "Completed")` once. This is the
frappe/payments convention, so apps written for it settle orders unchanged.

### Payment flow

1. Caller inserts a **Gateway Payment Request** (`ref_doctype`/`ref_docname`, `gateway` = profile name,
   `amount`, `currency_code`, optional customer fields).
2. `before_save` calls `create_session` once (when `order_ref` is empty) and stores `order_ref` (gateway
   session id, unique), `order_url` (checkout URL for the shopper) and the return URLs.
3. Status moves from `Pending` by one of two paths, both under a row lock (`lock_refund_ledger`):
   - **Webhook**: `bwh_payments.bwh_payments.webhook.handle` (guest POST, `?gateway=<profile>`,
     rate-limited) → settings `handle_webhook` verifies and returns a `WebhookEvent` or `None`
     → request found by `order_ref` → `apply_webhook_status` as Administrator (dedupes on
     `last_webhook_event_id`, only writes from `Pending`).
   - **Pull**: whitelisted `sync_status` asks the gateway. Return pages must call this; never trust the
     browser redirect.
4. `refund(amount=None, payment_entry=None)` does full or partial refunds with an over-refund guard, keeps
   `refund_amount`, a comma-joined `refund_id` list, and sets `Partially Refunded` / `Refunded`.
5. ERPNext bridge (`hooks.py` `doc_events`): submitting a `Payment Entry` with `payment_type = "Pay"`,
   `reference_no` = `order_ref` and `mode_of_payment` = the gateway profile name refunds through the
   gateway. Amended entries are traced back via `amended_from` so a refund is never sent twice.

### Gateway-specific behavior worth knowing

- **Stripe**: official SDK through a `stripe.StripeClient` per `LiveStripeClient` (never module-level
  `stripe.api_key`). SDK objects are attribute-based, so the live client parses with `from_attributes=True`.
  The `stripe` version range in `pyproject.toml` is constrained by `frappe/payments` in the same venv; read
  the comment there before changing it.
- **Razorpay**: raw HTTP (Payment Links API) via `frappe.integrations.utils.make_post_request` /
  `make_get_request`, no SDK.
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
`frappe.throw` from a verifier does not leak. Do not log webhook payloads; they can contain cardholder
data. Every call is recorded as an Integration Request via `create_request_log`.

## Tests

Tests are `IntegrationTestCase` and never touch the network. Controllers pick their stub client under
tests on their own, so tests do not patch transports: call `Stub<Service>Client.reset()` in `setUp`, drive
the outcome with its controls, and assert on its recordings. The real controller code runs every time.

Frappe 17's `IntegrationTestCase` rolls back only at class end, and `create_request_log` (webhook and refund
logging) commits. That is why the Stripe and Razorpay fixtures come with `remove_*_gateway` helpers that run
in `tearDownClass`.

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
- Deliberate shortcuts are marked with `# ponytail:` comments that name the limit and the upgrade path.
- `get_available_payment_modes` uses a per-worker `site_cache` (1 h TTL), so other workers can briefly
  keep offering a just-disabled gateway.
