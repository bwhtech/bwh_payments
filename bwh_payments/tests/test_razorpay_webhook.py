# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.utils.data import flt
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.base_class import WebhookEvent
from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RAZORPAY_WEBHOOK_SECRET,
	RazorpayTestCase,
	make_razorpay_payment_request,
)
from bwh_payments.services.razorpay.stub import (
	StubRazorpayClient,
	build_payment_link_event,
	build_refund_event,
	sign_payload,
)


class TestRazorpayWebhookSpine(RazorpayTestCase):
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
		payload = build_payment_link_event(payment_request.order_ref)

		response = self.post_webhook(payload, sign_payload(payload, RAZORPAY_WEBHOOK_SECRET))

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_forged_signature_is_rejected_and_nothing_is_marked_paid(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_event(payment_request.order_ref)

		response = self.post_webhook(payload, sign_payload(payload, "attacker_guess"))

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unsigned_delivery_is_rejected(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_event(payment_request.order_ref)

		response = self.post_webhook(payload, None)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_replayed_delivery_is_accepted_but_applied_once(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_event(payment_request.order_ref)
		signature = sign_payload(payload, RAZORPAY_WEBHOOK_SECRET)

		self.post_webhook(payload, signature, event_id="evt_rzp_replay")
		second = self.post_webhook(payload, signature, event_id="evt_rzp_replay")

		# Still a 200: anything else and Razorpay retries the same event forever.
		self.assertEqual(second["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.last_webhook_event_id, "evt_rzp_replay")

	def test_a_rejection_does_not_echo_the_gateway_error_back_to_the_caller(self):
		payload = build_payment_link_event("plink_nothing")

		self.post_webhook(payload, None)

		self.assertEqual(frappe.local.message_log, [])

	def test_the_webhook_log_never_stores_the_raw_payload(self):
		payment_request = make_razorpay_payment_request(100, "INR")
		payload = build_payment_link_event(payment_request.order_ref)

		self.post_webhook(payload, sign_payload(payload, RAZORPAY_WEBHOOK_SECRET))

		logged = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{RAZORPAY_GATEWAY} Webhook"},
			fields=["data"],
		)
		self.assertTrue(logged)
		for row in logged:
			self.assertNotIn("payment_link", row.data)
			self.assertNotIn("entity", row.data)

	def make_paid_request(self, amount: float = 100):
		payment_request = make_razorpay_payment_request(amount, "INR")
		StubRazorpayClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		return payment_request

	def post_refund_event(self, refund_id, payment_id, status, amount_in_paise, event_id):
		payload = build_refund_event(refund_id, payment_id, status, amount_in_paise)
		return self.post_webhook(payload, sign_payload(payload, RAZORPAY_WEBHOOK_SECRET), event_id=event_id)

	def test_a_processed_refund_event_settles_the_pending_refund(self):
		payment_request = self.make_paid_request()
		StubRazorpayClient.next_refund_status = "pending"
		refund = payment_request.create_refund(40)

		response = self.post_refund_event(
			refund.refund_id, payment_request.gateway_transaction_ref, "processed", 4000, "evt_rfnd_processed"
		)

		self.assertEqual(response["status"], "ok")
		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(flt(payment_request.refund_amount), 40)

	def test_a_failed_refund_event_frees_the_amount_again(self):
		payment_request = self.make_paid_request()
		StubRazorpayClient.next_refund_status = "pending"
		refund = payment_request.create_refund(100)

		self.post_refund_event(
			refund.refund_id, payment_request.gateway_transaction_ref, "failed", 10000, "evt_rfnd_failed"
		)

		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Failed")
		self.assertEqual(payment_request.get_remaining_refundable_amount(), 100)

	def test_a_refund_raised_in_the_razorpay_dashboard_is_recorded_against_its_payment(self):
		payment_request = self.make_paid_request()

		self.post_refund_event(
			"rfnd_from_dashboard", payment_request.gateway_transaction_ref, "processed", 2500, "evt_rfnd_ext"
		)

		[refund] = frappe.get_all(
			"Gateway Refund",
			filters={"payment_request": payment_request.name},
			fields=["refund_id", "status", "amount"],
		)
		self.assertEqual(
			(refund.refund_id, refund.status, flt(refund.amount)), ("rfnd_from_dashboard", "Processed", 25)
		)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_a_refund_for_a_payment_we_never_took_is_acknowledged_and_ignored(self):
		response = self.post_refund_event(
			"rfnd_stranger", "pay_stranger", "processed", 100, "evt_rfnd_stranger"
		)

		self.assertEqual(response["status"], "ok")
		self.assertIsNone(self.get_status_code())
		self.assertFalse(frappe.db.exists("Gateway Refund", {"refund_id": "rfnd_stranger"}))

	def test_a_refund_event_that_races_our_own_refund_is_retried_rather_than_counted_twice(self):
		payment_request = self.make_paid_request()
		# Our refund is reserved but the gateway has not answered yet, so it has no refund id.
		frappe.get_doc(
			{"doctype": "Gateway Refund", "payment_request": payment_request.name, "amount": 25}
		).insert(ignore_permissions=True)
		event = WebhookEvent(
			refund_id="rfnd_in_flight",
			status="processed",
			refund_amount=25,
			transaction_ref=payment_request.gateway_transaction_ref,
		)

		with self.assertRaises(frappe.RetryBackgroundJobError):
			webhook.apply_refund_event(payment_request, event)

		self.assertFalse(frappe.db.exists("Gateway Refund", {"refund_id": "rfnd_in_flight"}))

	def test_an_event_naming_neither_a_session_nor_a_refund_is_invalid(self):
		with self.assertRaises(ValueError):
			WebhookEvent(status="processed")
