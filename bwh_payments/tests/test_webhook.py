# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	GATEWAY,
	WEBHOOK_SECRET,
	configure_stripe_gateway,
	make_payment_request,
)
from bwh_payments.bwh_payments.doctype.stripe_gateway_settings import stripe_gateway_settings
from bwh_payments.tests.fake_stripe import (
	FakeStripeClient,
	build_checkout_completed_event,
	sign_stripe_payload,
)


class TestWebhook(IntegrationTestCase):
	def setUp(self):
		FakeStripeClient.reset()
		self.stripe_client_patch = patch.object(
			stripe_gateway_settings.stripe, "StripeClient", FakeStripeClient
		)
		self.stripe_client_patch.start()
		self.addCleanup(self.stripe_client_patch.stop)
		configure_stripe_gateway()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	def restore_request(self):
		frappe.local.request = self.original_request

	def post_webhook(self, gateway: str, payload: bytes, signature: str | None):
		headers = {"Content-Type": "application/json"}
		if signature is not None:
			headers["Stripe-Signature"] = signature
		builder = EnvironBuilder(
			method="POST", path="/api/method/bwh_payments.bwh_payments.webhook.handle", data=payload
		)
		builder.headers.extend(headers)
		environ = builder.get_environ()
		environ["QUERY_STRING"] = f"gateway={gateway}"
		frappe.local.request = Request(environ)
		# Normally set by the WSGI handler; the rate limiter counts against it.
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def get_status_code(self):
		return frappe.local.response.get("http_status_code")

	def test_a_correctly_signed_event_marks_the_request_paid(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref)
		FakeStripeClient.register_paid_session(payment_request.order_ref)

		response = self.post_webhook(GATEWAY, payload, sign_stripe_payload(payload, WEBHOOK_SECRET))

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_forged_signature_is_rejected_and_nothing_is_marked_paid(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref)
		forged = sign_stripe_payload(payload, "whsec_attacker_guess")

		response = self.post_webhook(GATEWAY, payload, forged)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unsigned_delivery_is_rejected(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref)

		response = self.post_webhook(GATEWAY, payload, None)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unknown_gateway_is_rejected(self):
		payload = build_checkout_completed_event("cs_test_nothing")

		response = self.post_webhook("Not A Gateway", payload, sign_stripe_payload(payload, WEBHOOK_SECRET))

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)

	def test_every_verification_failure_answers_with_the_same_opaque_400(self):
		"""Distinguishable replies let an unauthenticated caller enumerate the configured gateways."""
		payload = build_checkout_completed_event("cs_test_nothing")

		unknown_gateway = self.post_webhook("Not A Gateway", payload, None)
		unsigned = self.post_webhook(GATEWAY, payload, None)
		forged = self.post_webhook(GATEWAY, payload, sign_stripe_payload(payload, "whsec_attacker_guess"))

		self.assertEqual(unknown_gateway, unsigned)
		self.assertEqual(unsigned, forged)
		self.assertEqual(self.get_status_code(), 400)

	def test_a_rejection_does_not_echo_the_gateway_error_back_to_the_caller(self):
		"""frappe.throw inside a gateway verifier lands in _server_messages unless the log is cleared."""
		payload = build_checkout_completed_event("cs_test_nothing")

		self.post_webhook(GATEWAY, payload, None)

		self.assertEqual(frappe.local.message_log, [])

	def test_a_replayed_delivery_is_accepted_but_applied_once(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref, event_id="evt_replay")
		signature = sign_stripe_payload(payload, WEBHOOK_SECRET)
		FakeStripeClient.register_paid_session(payment_request.order_ref)

		self.post_webhook(GATEWAY, payload, signature)
		second = self.post_webhook(GATEWAY, payload, signature)

		# Still a 200: anything else and Stripe retries the same event forever.
		self.assertEqual(second["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.last_webhook_event_id, "evt_replay")

	def test_the_webhook_log_never_stores_the_raw_payload(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref, event_id="evt_privacy")
		FakeStripeClient.register_paid_session(payment_request.order_ref)

		self.post_webhook(GATEWAY, payload, sign_stripe_payload(payload, WEBHOOK_SECRET))

		logged = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{GATEWAY} Webhook"},
			fields=["data"],
		)
		self.assertTrue(logged)
		for row in logged:
			self.assertNotIn("payment_status", row.data)
