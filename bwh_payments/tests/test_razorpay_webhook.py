# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RAZORPAY_WEBHOOK_SECRET,
	RazorpayTestCase,
	make_razorpay_payment_request,
)
from bwh_payments.tests.fake_razorpay import build_payment_link_paid_event, sign_razorpay_payload

# No IGNORE_TEST_RECORD_DEPENDENCIES here: frappe only honours it inside a doctype folder and raises
# NotImplementedError otherwise. Outside one it loads no test records at all, which is what we want.


class TestRazorpayWebhookSpine(RazorpayTestCase):
	"""The whole delivery path: `webhook.handle()` -> gateway verifier -> Gateway Payment Request."""

	def setUp(self):
		super().setUp()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	def restore_request(self):
		frappe.local.request = self.original_request

	def post_webhook(self, payload: bytes, signature: str | None, event_id: str = "evt_rzp_spine"):
		headers = {"Content-Type": "application/json", "X-Razorpay-Event-Id": event_id}
		if signature is not None:
			headers["X-Razorpay-Signature"] = signature
		builder = EnvironBuilder(
			method="POST", path="/api/method/bwh_payments.bwh_payments.webhook.handle", data=payload
		)
		builder.headers.extend(headers)
		environ = builder.get_environ()
		environ["QUERY_STRING"] = f"gateway={RAZORPAY_GATEWAY}"
		frappe.local.request = Request(environ)
		# Normally set by the WSGI handler; the rate limiter counts against it.
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def get_status_code(self):
		return frappe.local.response.get("http_status_code")

	def test_a_correctly_signed_event_marks_the_request_paid(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_paid_event(payment_request.order_ref)

		response = self.post_webhook(payload, sign_razorpay_payload(payload, RAZORPAY_WEBHOOK_SECRET))

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_forged_signature_is_rejected_and_nothing_is_marked_paid(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_paid_event(payment_request.order_ref)

		response = self.post_webhook(payload, sign_razorpay_payload(payload, "attacker_guess"))

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unsigned_delivery_is_rejected(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_paid_event(payment_request.order_ref)

		response = self.post_webhook(payload, None)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_replayed_delivery_is_accepted_but_applied_once(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_paid_event(payment_request.order_ref)
		signature = sign_razorpay_payload(payload, RAZORPAY_WEBHOOK_SECRET)

		self.post_webhook(payload, signature, event_id="evt_rzp_replay")
		second = self.post_webhook(payload, signature, event_id="evt_rzp_replay")

		# Still a 200: anything else and Razorpay retries the same event forever.
		self.assertEqual(second["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.last_webhook_event_id, "evt_rzp_replay")

	def test_a_rejection_does_not_echo_the_gateway_error_back_to_the_caller(self):
		payload = build_payment_link_paid_event("plink_nothing")

		self.post_webhook(payload, None)

		self.assertEqual(frappe.local.message_log, [])

	def test_the_webhook_log_never_stores_the_raw_payload(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_paid_event(payment_request.order_ref)

		self.post_webhook(payload, sign_razorpay_payload(payload, RAZORPAY_WEBHOOK_SECRET))

		logged = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{RAZORPAY_GATEWAY} Webhook"},
			fields=["data"],
		)
		self.assertTrue(logged)
		# The log records the gateway, the link id and the event id — never the delivered body.
		for row in logged:
			self.assertNotIn("payment_link", row.data)
			self.assertNotIn("entity", row.data)
