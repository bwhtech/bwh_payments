# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.paypal_gateway_settings.test_paypal_gateway_settings import (
	PAYPAL_GATEWAY,
	PayPalTestCase,
	make_paypal_payment_request,
)
from bwh_payments.tests.fake_paypal import (
	SIGNATURE_HEADERS,
	FakePayPal,
	build_capture_completed_event,
	build_order_approved_event,
)

# No IGNORE_TEST_RECORD_DEPENDENCIES here: frappe only honours it inside a doctype folder and raises
# NotImplementedError otherwise. Outside one it loads no test records at all, which is what we want.


class TestPayPalWebhookSpine(PayPalTestCase):
	"""The whole delivery path: `webhook.handle()` -> gateway verifier -> Gateway Payment Request."""

	def setUp(self):
		super().setUp()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	def restore_request(self):
		frappe.local.request = self.original_request

	def post_webhook(self, payload: bytes, headers: dict | None = None, gateway: str = PAYPAL_GATEWAY):
		builder = EnvironBuilder(
			method="POST", path="/api/method/bwh_payments.bwh_payments.webhook.handle", data=payload
		)
		# `headers or SIGNATURE_HEADERS` would treat the empty dict a test passes to strip the signature
		# as "not given" and sign the delivery anyway.
		signed = SIGNATURE_HEADERS if headers is None else headers
		builder.headers.extend({"Content-Type": "application/json", **signed})
		environ = builder.get_environ()
		environ["QUERY_STRING"] = f"gateway={gateway}"
		frappe.local.request = Request(environ)
		# Normally set by the WSGI handler; the rate limiter counts against it.
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def get_status_code(self):
		return frappe.local.response.get("http_status_code")

	def test_a_capture_completed_event_marks_the_request_paid(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)

		response = self.post_webhook(payload)

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_an_approved_event_captures_the_money_the_shopper_never_came_back_for(self):
		payment_request = make_paypal_payment_request(100, "USD")
		FakePayPal.orders[payment_request.order_ref]["status"] = "APPROVED"
		payload = build_order_approved_event(payment_request.order_ref)

		response = self.post_webhook(payload)

		# The whole point of subscribing to this event: a shopper who approves on PayPal and closes the
		# tab is charged anyway, instead of the order sitting approved forever.
		self.assertEqual(response["status"], "ok")
		self.assertEqual(FakePayPal.captured_orders, [payment_request.order_ref])
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_delivery_paypal_will_not_verify_is_rejected(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)
		FakePayPal.next_verification_status = "FAILURE"

		response = self.post_webhook(payload)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_delivery_missing_its_signature_headers_is_rejected(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)

		response = self.post_webhook(payload, headers={})

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")
		# Nothing was even asked of PayPal: a delivery without the headers cannot be verified at all.
		self.assertEqual(FakePayPal.verified_events, [])

	def test_the_body_is_handed_to_paypal_exactly_as_the_event_it_arrived_as(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref, event_id="WH-VERIFY-0001")

		self.post_webhook(payload)

		verified = FakePayPal.verified_events[0]
		# PayPal signs the event it sent, so verification has to hand back that same event alongside the
		# five transmission headers and our webhook id.
		self.assertEqual(verified["webhook_event"]["id"], "WH-VERIFY-0001")
		self.assertEqual(verified["transmission_id"], SIGNATURE_HEADERS["Paypal-Transmission-Id"])
		self.assertEqual(verified["webhook_id"], self.get_settings().webhook_id)

	def test_an_event_type_we_do_not_act_on_is_accepted_and_ignored(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref).replace(
			b"PAYMENT.CAPTURE.COMPLETED", b"PAYMENT.CAPTURE.PENDING"
		)

		response = self.post_webhook(payload)

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_replayed_delivery_is_accepted_but_applied_once(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref, event_id="WH-REPLAY-0001")

		self.post_webhook(payload)
		second = self.post_webhook(payload)

		# Still a 200: anything else and PayPal retries the same event for days.
		self.assertEqual(second["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.last_webhook_event_id, "WH-REPLAY-0001")

	def test_every_failure_looks_the_same_from_outside(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)

		unknown_gateway = self.post_webhook(payload, gateway="NotAGateway")
		unsigned = self.post_webhook(payload, headers={})
		FakePayPal.next_verification_status = "FAILURE"
		unverified = self.post_webhook(payload)

		# A caller who can tell these apart can enumerate which gateways this site has configured and
		# probe how far a forged payload gets.
		self.assertEqual(unknown_gateway, unsigned)
		self.assertEqual(unsigned, unverified)

	def test_a_rejection_does_not_echo_the_gateway_error_back_to_the_caller(self):
		payload = build_capture_completed_event("PAYPALNOTHING")

		self.post_webhook(payload, headers={})

		self.assertEqual(frappe.local.message_log, [])

	def test_the_webhook_log_never_stores_the_raw_payload(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)

		self.post_webhook(payload)

		logged = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{PAYPAL_GATEWAY} Webhook"},
			fields=["data"],
		)
		self.assertTrue(logged)
		# The log records the gateway, the order id and the event id — never the delivered body.
		for row in logged:
			self.assertNotIn("supplementary_data", row.data)
			self.assertNotIn("event_type", row.data)
