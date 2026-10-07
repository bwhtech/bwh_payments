---
name: migrate-payment-gateway
description: Move an existing bwh_payments gateway (or any Frappe integration controller that calls make_post_request / make_get_request / an SDK directly) onto the base/live/stub service-client layout in bwh_payments/services/GATEWAY/, with typed pydantic models and tests that drive a stub instead of patching transports. Use this whenever someone wants to refactor, restructure, "clean up", make testable, or "move to the services pattern" a Gateway Settings controller, delete a tests/fake_*.py, stop patching HTTP in tests, or add stubs for a payment, shipping or other third-party integration, even if they don't say "migrate".
---

# Migrate a gateway to base/live/stub service clients

The target layout and its rules are in `AGENTS.md` under "Service clients (base / live / stub)". Read that
section first; it is the source of truth, and this skill is the procedure for getting there safely. The
four gateways already migrated (`bwh_payments/services/{razorpay,stripe,telr,tabby}/`) are worked
examples. Razorpay (raw HTTP) and Stripe (SDK) are the cleanest to copy from.

The whole point of the migration is that **behaviour does not change**. So the procedure is built around
proving that: pin current behaviour, move code in thin slices, and re-run the same tests after each slice.

## 0. Set up

- Branch in **every** repo you will touch (bwh_payments and each consumer app such as commera) before the
  first edit.
- Integration tests need the bench's Redis. If they crash with `Error 61 connecting to 127.0.0.1:<port>`,
  start it from the bench root: `redis-server config/redis_cache.conf --daemonize yes`, and the same for
  `config/redis_queue.conf`. Or use `bench start`.
- Run the baseline and write the counts down:
  `bench --site <site> run-tests --app bwh_payments`, then the same for each consumer app. You will
  compare against these numbers at the end.

## 1. Pin behaviour before touching anything

Look at what the gateway's existing tests and the consumer apps' tests actually exercise. If a consumer
(commera, buzz) has no end-to-end test through this gateway, write one first, against the old code:
checkout, then paid via webhook, then paid via the return page, then abandoned-cart release, then refund.
`commera/tests/test_razorpay_checkout.py` is the template.

- Put every reference to the transport fake in a small block of "gateway driver" helpers (`shopper_pays`,
  `created_links`, `refunds_sent`, `link_status`, `checkout_url`). The migration may change those
  helper bodies and nothing else. That is the parity proof.
- Prove the test can fail. Temporarily break one decision in the controller (for example map `paid` to
  `Pending`), run the test, see it go red, then restore the file with `git checkout -- <file>`.

## 2. Sort every line of the controller into transport or decision

Read the controller top to bottom and classify:

| Transport: moves to `live.py` | Decision: stays in the controller |
|---|---|
| base URL, auth headers, Basic/Bearer tokens | status maps (unknown → `Pending`, never `Paid`) |
| `make_post_request` / `make_get_request` / SDK calls | currency → minor units, decimal formatting |
| reading error bodies, non-JSON guards, `frappe.throw` on refusal | success/return URL building |
| `create_request_log` and summarising for logs | capture and refund rules, "authorised counts as paid" switches |
| request encoding (e.g. Telr's refund XML) and response parsing | webhook signature/token verification |

Webhook verification always stays in the controller. It is local crypto, and if it lived in a client, the
stub that replaces the client under tests would accept forged events.

## 3. Write `services/<gateway>/base.py`

- One abstract method per gateway call the controller makes. Name it after the gateway's own operation
  (`create_payment_link`, `check_order`, `capture_payment`). Pass request bodies in the gateway's own
  shape (`payload: dict`) when the controller builds them, so recordings stay comparable to the docs.
- Models mirror the gateway's JSON, keeping only fields the controller reads. Subclass
  `bwh_payments.services.GatewayModel`, which ignores unknown fields and coerces bare numbers into `str`
  fields, so an id the gateway sometimes sends as `1234567` still parses. Use nested models where the JSON
  nests, and `| None = None` wherever the gateway may omit a field or send null. Use
  `str | float | None` for amounts that arrive both as `100` and as `"100.00"`.
- The docstring on the ABC says what a refusal raises (`frappe.ValidationError`, or the SDK's own error
  type when the controller already catches that, as with Stripe).

## 4. Write `live.py`

Move the transport code out of the controller. Live clients are deliberately untested, so keep them to
transport only: call, log, translate errors, parse. If you catch yourself writing an `if` about payment
state here, it belongs in the controller.

- Credentials go in the constructor. A secret that is optional on the settings (Telr's `remote_auth_key`)
  goes as an argument to the one method that needs it. `get_password` raises when the field is empty,
  and `get_client()` runs on every call.
- Log against the settings Single (`reference_doctype = reference_docname = "<Name> Gateway Settings"`).
  Log endpoints and ids only, never payloads: they carry keys and shopper contact details.
- SDK objects: Stripe SDK 15 objects are attribute-based, not dicts, so parse with
  `Model.model_validate(obj, from_attributes=True)`. Call the SDK's current namespace (`StripeClient.v1`,
  from 12.5); the deprecated top-level services warn on every call.
- Smoke-test the parsing offline: feed each model a payload shaped like the provider's documented
  response, including null blocks and numeric ids. Live clients have no tests, so this is the only check
  their parsing gets.

## 5. Write `stub.py`

An in-memory version of the gateway that behaves like it, not a mock that returns whatever a test wants.

- State in `ClassVar`s, a `reset()` classmethod, ids from `frappe.generate_hash` (the `order_ref` column is
  unique, and rows can outlive a test).
- Name the controls after real-world events: `pay()`, `authorise()`, `set_status()`, `add_<thing>()` for
  objects that exist at the gateway without this app creating them, and `next_refund_status` for refusals.
- Recordings (`created_*`, `refunds`, `captures`) keep the gateway's request shape.
- Unknown ids and refused operations raise the same exception type the live client raises, with the same
  message prefix. Controllers catch these (for example `cancel_session` returning `False`).
- Return copies (`model_copy(deep=True)`) so the controller cannot mutate the stub's store.
- Webhook signing and event builders for tests live in `stub.py` too, replacing the ones in
  `tests/fake_*.py`.

## 6. Rewire the controller, in a tracer slice first

```python
def get_client(self) -> BaseXClient:
	if frappe.in_test:
		return StubXClient()
	return LiveXClient(self.key_id, self.get_password("key_secret"))
```

Rewire `create_session` alone first and run the gateway's tests. Failures that only come from tests still
reading the old fake are expected, and they show the stub path works end to end. Then rewire the rest:

- `create_session` returns `CheckoutSession`, `refund_payment` returns `RefundResult`, and
  `handle_webhook` returns `WebhookEvent | None` (`None` replaces the old `{}` for "ignore this delivery").
- Helpers that took response dicts now take models. Replace `payment.get("status")` with
  `payment.status`, and `.get("payments") or []` with `payment_link.payments or []`.
- Keep `frappe.in_test` as the only switch. No env var, no "no keys configured" fallback: on a real
  site that would mark orders Paid with no money taken.

## 7. Move the tests onto the stub

- In `setUp`: `StubXClient.reset()`. Delete the `patch.object(..., "make_post_request", ...)` blocks.
- Replace fake reads with stub controls and recordings, one for one. Keep every assertion's meaning. If a
  test patched a controller method that no longer exists (Telr's `get_order`), drive the same scenario
  through stub controls instead.
- Result dict access becomes attribute access (`session["session_id"]` becomes `session.session_id`), and
  `assertEqual(result, {})` becomes `assertIsNone(result)`.
- A gateway with no tests gets them now. The stub makes them cheap (see the Tabby tests).

## 8. Fix consumers in the same change

```bash
grep -rn "fake_<gateway>\|Fake<Gateway>\|<gateway>_gateway_settings\.\(stripe\|make_\)" --include='*.py' apps/
```

Swap consumer tests one for one, with no assertion changes, and commit them separately in the consumer
repo. Say in the message which bwh_payments commit they need, because merge order matters.

Also grep every app on the bench for controller methods you removed (`get_order`, `get_headers`, `post`,
`get_resource`) before deleting them.

## 9. Delete the fake and verify

- `git rm bwh_payments/tests/fake_<gateway>.py`.
- Run both full suites. Counts must match the baseline plus any tests you added, with nothing skipped that
  wasn't skipped before.
- Run the consumer parity test. Its diff should touch only imports and the driver helpers.
- Mutation-check one decision again, so the tests still bite with the stub in place.
- `pre-commit run --files <changed files>`, twice if ruff-format rewrites something.
- Leak check: no Payment Gateway Profile, enabled settings or Integration Requests left behind on the site.

## Test-harness gotchas (Frappe 17)

- `IntegrationTestCase` rolls back only at **class** end, and `create_request_log` calls
  `frappe.db.commit()`. A test class that logs (webhooks, refunds, live calls) either cleans up in
  `tearDownClass` (`remove_*_gateway` helpers) or patches `frappe.db.commit` for the class and rolls back
  itself.
- Importing another `TestCase` class by name into a test module makes unittest run its tests a second
  time. Borrow its helpers inside your class body (`helper = OtherSuite.helper`, then `del OtherSuite`).
- `frappe.clear_cache()` with no arguments also empties the per-worker `site_cache`, including
  `get_available_payment_modes`.

## Commit shape

One commit per gateway in bwh_payments (`refactor: move <Gateway> onto base/live/stub service clients`),
one matching commit in each consumer, and AGENTS.md updated when a rule changes.
