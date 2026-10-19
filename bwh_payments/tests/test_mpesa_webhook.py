# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.utils.data import flt

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.mpesa_gateway_settings.test_mpesa_gateway_settings import (
	MPESA_CALLBACK_TOKEN,
	MPESA_GATEWAY,
	MpesaTestCase,
	make_mpesa_payment_request,
	set_callback_request,
)
from bwh_payments.services.mpesa.stub import StubMpesaClient, build_reversal_result, build_stk_callback


class TestMpesaWebhookSpine(MpesaTestCase):
	def post_callback(self, payload: bytes, token: str | None = MPESA_CALLBACK_TOKEN):
		set_callback_request(token, payload)
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def get_status_code(self):
		return frappe.local.response.get("http_status_code")

	def test_a_paid_callback_marks_the_request_paid_and_keeps_the_receipt(self):
		payment_request = make_mpesa_payment_request()
		receipt = StubMpesaClient.pay(payment_request.order_ref)

		response = self.post_callback(build_stk_callback(payment_request.order_ref, receipt=receipt))

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.gateway_transaction_ref, receipt)

	def test_a_callback_after_a_status_sync_still_keeps_the_receipt(self):
		payment_request = make_mpesa_payment_request()
		receipt = StubMpesaClient.pay(payment_request.order_ref)
		payment_request.sync_status()

		self.post_callback(build_stk_callback(payment_request.order_ref, receipt=receipt))

		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.gateway_transaction_ref, receipt)

	def test_a_cancelled_callback_cancels_the_request(self):
		payment_request = make_mpesa_payment_request()

		self.post_callback(build_stk_callback(payment_request.order_ref, result_code=1032))

		payment_request.reload()
		self.assertEqual(payment_request.status, "Cancelled")
		self.assertIsNone(payment_request.gateway_transaction_ref)

	def test_a_callback_with_the_wrong_token_is_rejected_and_nothing_is_marked_paid(self):
		payment_request = make_mpesa_payment_request()

		response = self.post_callback(build_stk_callback(payment_request.order_ref), token="attacker_guess")

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_replayed_callback_is_accepted_but_applied_once(self):
		payment_request = make_mpesa_payment_request()
		payload = build_stk_callback(payment_request.order_ref, receipt="SREPLAY001")

		self.post_callback(payload)
		second = self.post_callback(payload)

		self.assertEqual(second["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.last_webhook_event_id, payment_request.order_ref)

	def test_the_webhook_log_never_stores_the_raw_payload(self):
		payment_request = make_mpesa_payment_request()

		self.post_callback(build_stk_callback(payment_request.order_ref, receipt="SNOLOG0001"))

		logged = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{MPESA_GATEWAY} Webhook"},
			fields=["data"],
		)
		self.assertTrue(logged)
		for row in logged:
			self.assertNotIn("PhoneNumber", row.data)
			self.assertNotIn("254708374149", row.data)

	def make_paid_request(self):
		payment_request = make_mpesa_payment_request()
		receipt = StubMpesaClient.pay(payment_request.order_ref)
		self.post_callback(build_stk_callback(payment_request.order_ref, receipt=receipt))
		payment_request.reload()
		return payment_request

	def test_a_successful_reversal_result_settles_the_pending_refund(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(40)

		response = self.post_callback(build_reversal_result(refund.refund_id))

		self.assertEqual(response["status"], "ok")
		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(flt(payment_request.refund_amount), 40)
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_a_failed_reversal_result_frees_the_amount_again(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(100)

		self.post_callback(build_reversal_result(refund.refund_id, result_code=2001))

		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Failed")
		self.assertEqual(payment_request.get_remaining_refundable_amount(), 100)

	def test_a_reversal_result_for_a_refund_we_never_made_is_acknowledged_and_ignored(self):
		response = self.post_callback(build_reversal_result("AG_stranger"))

		self.assertEqual(response["status"], "ok")
		self.assertIsNone(self.get_status_code())
		self.assertFalse(frappe.db.exists("Gateway Refund", {"refund_id": "AG_stranger"}))
