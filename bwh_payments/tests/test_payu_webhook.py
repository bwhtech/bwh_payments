# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.utils.data import flt
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.payu_gateway_settings.test_payu_gateway_settings import (
	PAYU_GATEWAY,
	PAYU_KEY,
	PAYU_SALT,
	PayUTestCase,
	make_payu_payment_request,
)
from bwh_payments.services.payu.base import PayURefund
from bwh_payments.services.payu.stub import StubPayUClient, build_payment_event, build_refund_event


class TestPayUWebhookSpine(PayUTestCase):
	def post_webhook(self, payload: bytes, content_type: str = "application/x-www-form-urlencoded"):
		builder = EnvironBuilder(
			method="POST",
			path="/api/method/bwh_payments.bwh_payments.webhook.handle",
			data=payload,
			content_type=content_type,
		)
		environ = builder.get_environ()
		environ["QUERY_STRING"] = f"gateway={PAYU_GATEWAY}"
		frappe.local.request = Request(environ)
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def test_a_signed_payment_event_marks_the_request_paid(self):
		payment_request = make_payu_payment_request(100)
		StubPayUClient.pay(payment_request.order_ref)

		response = self.post_webhook(build_payment_event(payment_request.order_ref, PAYU_KEY, PAYU_SALT))

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_forged_payment_event_is_rejected_and_nothing_is_marked_paid(self):
		payment_request = make_payu_payment_request(100)

		response = self.post_webhook(build_payment_event(payment_request.order_ref, PAYU_KEY, "attacker"))

		self.assertEqual(response["status"], "error")
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_refund_event_settles_the_pending_refund(self):
		payment_request = make_payu_payment_request(100)
		StubPayUClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		refund = payment_request.create_refund(40)
		StubPayUClient.set_refund_status(refund.refund_id, "SUCCESS")

		response = self.post_webhook(build_refund_event(refund.refund_id), "application/json")

		self.assertEqual(response["status"], "ok")
		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(flt(payment_request.refund_amount), 40)

	def test_a_dashboard_refund_without_an_amount_is_not_recorded_as_zero(self):
		payment_request = make_payu_payment_request(100)
		mihpayid = StubPayUClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		StubPayUClient.refunds["rfnd_dashboard"] = PayURefund(
			request_id="rfnd_dashboard", status="SUCCESS", mihpayid=mihpayid
		)

		self.post_webhook(build_refund_event("rfnd_dashboard"), "application/json")

		self.assertFalse(frappe.db.exists("Gateway Refund", {"refund_id": "rfnd_dashboard"}))
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
