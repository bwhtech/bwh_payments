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
from bwh_payments.services.paypal.stub import (
	SIGNATURE_HEADERS,
	StubPayPalClient,
	build_capture_completed_event,
	build_order_approved_event,
	build_refund,
	build_refund_event,
)


class PayPalWebhookTestCase(PayPalTestCase):
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
		# `is None`, not `or`: an empty dict means "strip the signature".
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


class TestPayPalWebhookSpine(PayPalWebhookTestCase):
	def test_a_capture_completed_event_marks_the_request_paid(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)

		response = self.post_webhook(payload)

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_an_approved_event_captures_the_money_the_shopper_never_came_back_for(self):
		payment_request = make_paypal_payment_request(100, "USD")
		StubPayPalClient.approve(payment_request.order_ref)
		payload = build_order_approved_event(payment_request.order_ref)

		response = self.post_webhook(payload)

		self.assertEqual(response["status"], "ok")
		self.assertEqual(StubPayPalClient.captured_orders, [payment_request.order_ref])
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_an_approved_order_that_is_not_ours_is_never_captured(self):
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.approve(order_id)

		response = self.post_webhook(build_order_approved_event(order_id))

		self.assertEqual(response["status"], "ok")
		self.assertEqual(StubPayPalClient.captured_orders, [])

	def test_an_approved_event_for_a_paid_request_captures_nothing(self):
		payment_request = make_paypal_payment_request(100, "USD")
		StubPayPalClient.pay(payment_request.order_ref)
		payment_request.sync_status()

		self.post_webhook(build_order_approved_event(payment_request.order_ref, event_id="WH-LATE-0001"))

		self.assertEqual(StubPayPalClient.captured_orders, [])

	def test_a_delivery_paypal_will_not_verify_is_rejected(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref)
		StubPayPalClient.next_verification_status = "FAILURE"

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
		self.assertEqual(StubPayPalClient.verified_events, [])

	def test_the_body_is_handed_to_paypal_exactly_as_the_event_it_arrived_as(self):
		payment_request = make_paypal_payment_request(100, "USD")
		payload = build_capture_completed_event(payment_request.order_ref, event_id="WH-VERIFY-0001")

		self.post_webhook(payload)

		verified = StubPayPalClient.verified_events[0]
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
		StubPayPalClient.next_verification_status = "FAILURE"
		unverified = self.post_webhook(payload)

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
		for row in logged:
			self.assertNotIn("supplementary_data", row.data)
			self.assertNotIn("event_type", row.data)


class TestPayPalRefundWebhook(PayPalWebhookTestCase):
	def make_paid_request(self, amount: float = 20):
		payment_request = make_paypal_payment_request(amount, "USD")
		StubPayPalClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		return payment_request

	def get_refund(self, refund_id: str):
		return frappe.get_doc("Gateway Refund", {"refund_id": refund_id})

	def test_a_refund_raised_on_the_paypal_dashboard_is_recorded(self):
		payment_request = self.make_paid_request()
		refund = build_refund(payment_request.gateway_transaction_ref, "COMPLETED", "5.00")

		response = self.post_webhook(build_refund_event(refund))

		self.assertEqual(response["status"], "ok")
		recorded = self.get_refund(refund.id)
		self.assertEqual(recorded.status, "Processed")
		self.assertEqual(recorded.amount, 5)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_a_pending_refund_settles_when_paypal_completes_it(self):
		payment_request = self.make_paid_request()
		StubPayPalClient.next_refund_status = "PENDING"
		refund_id = payment_request.create_refund(5).refund_id

		StubPayPalClient.set_refund_status(refund_id, "COMPLETED")
		self.post_webhook(build_refund_event(StubPayPalClient.issued_refunds[refund_id]))

		self.assertEqual(self.get_refund(refund_id).status, "Processed")

	def test_a_failed_refund_is_marked_failed(self):
		payment_request = self.make_paid_request()
		StubPayPalClient.next_refund_status = "PENDING"
		refund_id = payment_request.create_refund(5).refund_id

		StubPayPalClient.set_refund_status(refund_id, "FAILED")
		self.post_webhook(
			build_refund_event(StubPayPalClient.issued_refunds[refund_id], event_type="PAYMENT.REFUND.FAILED")
		)

		self.assertEqual(self.get_refund(refund_id).status, "Failed")

	def test_a_reversal_is_recorded_as_a_refund(self):
		payment_request = self.make_paid_request(20)
		refund = build_refund(payment_request.gateway_transaction_ref, "COMPLETED", "20.00")

		self.post_webhook(build_refund_event(refund, event_type="PAYMENT.CAPTURE.REVERSED"))

		payment_request.reload()
		self.assertEqual(payment_request.status, "Refunded")
