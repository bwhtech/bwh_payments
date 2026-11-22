import json
from contextlib import nullcontext
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RazorpayTestCase,
	make_razorpay_payment_request,
)
from bwh_payments.bwh_payments.utils import without_commits
from bwh_payments.services import GatewayRefusedError, GatewayUnavailableError
from bwh_payments.services.razorpay.live import LiveRazorpayClient
from bwh_payments.services.razorpay.stub import StubRazorpayClient
from bwh_payments.testing import lose_next_refund_answer

REFUND_KEY = "a1b2c3d4e5"
# POST /payments/{id}/refund as Razorpay answers it, trimmed of fields nobody reads.
PROCESSED_REFUND = {"id": "rfnd_FP8QHiV938haTz", "amount": 20000, "status": "processed"}


def razorpay_response(status_code: int, body: dict) -> requests.Response:
	response = requests.Response()
	response.status_code = status_code
	response._content = json.dumps(body).encode()
	response.headers["content-type"] = "application/json"
	response.url = "https://api.razorpay.com/v1/payments/pay_x/refund"
	return response


def razorpay_error(status_code: int, description: str) -> requests.Response:
	return razorpay_response(
		status_code, {"error": {"code": "BAD_REQUEST_ERROR", "description": description}}
	)


class TestLiveRazorpayRefund(IntegrationTestCase):
	def setUp(self):
		# The live client logs each call, and logging commits.
		self.enterContext(without_commits())

	def refund(self, **answer):
		with patch("requests.Session.request", **answer) as request:
			LiveRazorpayClient("rzp_test_x", "rzp_test_secret").create_refund(
				"pay_x", 20000, idempotency_key=REFUND_KEY
			)
		return request

	def test_the_refund_carries_its_idempotency_key(self):
		request = self.refund(return_value=razorpay_response(200, PROCESSED_REFUND))
		self.assertEqual(request.call_args.kwargs["headers"]["X-Refund-Idempotency"], REFUND_KEY)
		self.assertEqual(request.call_args.kwargs["json"], {"amount": 20000})

	def test_a_lost_answer_is_unavailable_not_refused(self):
		for error in (requests.Timeout, requests.ConnectionError):
			with self.subTest(error=error), self.assertRaises(GatewayUnavailableError):
				self.refund(side_effect=error)

	def test_a_server_error_or_a_refund_still_in_progress_is_unavailable(self):
		for status_code in (409, 429, 500, 503):
			with self.subTest(status_code=status_code), self.assertRaises(GatewayUnavailableError):
				self.refund(return_value=razorpay_error(status_code, "Please try again"))

	def test_a_refused_refund_is_a_refusal(self):
		with self.assertRaises(GatewayRefusedError):
			self.refund(return_value=razorpay_error(400, "The refund amount exceeds the payment"))


class TestRazorpayLostRefundAnswer(RazorpayTestCase):
	def paid_request(self, amount: float):
		payment_request = make_razorpay_payment_request(amount)
		StubRazorpayClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		return payment_request

	def test_a_refund_whose_answer_was_lost_stays_reserved(self):
		payment_request = self.paid_request(1000)
		lose_next_refund_answer()

		with self.assertRaises(GatewayUnavailableError):
			payment_request.create_refund(200)

		refund = frappe.get_last_doc("Gateway Refund", {"payment_request": payment_request.name})
		self.assertEqual(refund.status, "Pending")
		self.assertEqual(payment_request.get_remaining_refundable_amount(), 800)

	def test_a_refund_whose_answer_was_lost_is_made_once_when_synced(self):
		payment_request = self.paid_request(1000)
		lose_next_refund_answer()
		with self.assertRaises(GatewayUnavailableError):
			payment_request.create_refund(200)

		payment_request.update_pending_refunds()

		refund = frappe.get_last_doc("Gateway Refund", {"payment_request": payment_request.name})
		self.assertEqual(len(StubRazorpayClient.created_refunds), 1)
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(list(StubRazorpayClient.refunds), [refund.refund_id])
		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 200)

	def test_a_full_refund_whose_answer_was_lost_is_made_once_when_synced(self):
		payment_request = self.paid_request(1000)
		lose_next_refund_answer()
		with self.assertRaises(GatewayUnavailableError):
			payment_request.create_refund(1000)
		self.assertEqual(
			StubRazorpayClient.payments[payment_request.gateway_transaction_ref]["status"], "refunded"
		)

		payment_request.update_pending_refunds()

		refund = frappe.get_last_doc("Gateway Refund", {"payment_request": payment_request.name})
		self.assertEqual(len(StubRazorpayClient.created_refunds), 1)
		self.assertEqual(refund.status, "Processed")
		payment_request.reload()
		self.assertEqual((flt(payment_request.refund_amount), payment_request.status), (1000, "Refunded"))

	def refund_through_live_razorpay(self, payment_request, amount: float, **answer):
		"""Only the HTTP call to Razorpay is faked, so the live client parses and logs the answer."""
		live_client = LiveRazorpayClient("rzp_test_x", "rzp_test_secret")
		with (
			patch.object(type(self.get_settings()), "get_client", return_value=live_client),
			patch("requests.Session.request", return_value=razorpay_response(200, PROCESSED_REFUND)),
			patch(**answer) if answer else nullcontext(),
		):
			return payment_request.create_refund(amount)

	def assert_reserved(self, payment_request, amount: float):
		refund = frappe.get_last_doc("Gateway Refund", {"payment_request": payment_request.name})
		self.assertEqual(refund.status, "Pending")
		self.assertEqual(
			payment_request.get_remaining_refundable_amount(), flt(payment_request.amount) - amount
		)

	def test_a_refund_razorpay_made_but_this_site_could_not_log_stays_reserved(self):
		payment_request = self.paid_request(1000)

		with self.assertRaises(frappe.QueryDeadlockError):
			self.refund_through_live_razorpay(
				payment_request,
				200,
				target="bwh_payments.services.razorpay.live.create_request_log",
				side_effect=frappe.QueryDeadlockError,
			)

		self.assert_reserved(payment_request, 200)

	def test_an_unreadable_refund_answer_stays_reserved(self):
		payment_request = self.paid_request(1000)
		unreadable = requests.Response()
		unreadable.status_code = 200
		unreadable._content = b"<html>OK</html>"
		unreadable.headers["content-type"] = "text/html"

		with self.assertRaises(frappe.ValidationError):
			self.refund_through_live_razorpay(
				payment_request, 200, target="requests.Session.request", return_value=unreadable
			)

		self.assert_reserved(payment_request, 200)

	def test_a_successful_refund_answer_without_a_known_status_stays_reserved(self):
		for body in ({"id": "rfnd_NoStatus01", "amount": 20000}, {**PROCESSED_REFUND, "status": "on_hold"}):
			with self.subTest(body=body):
				payment_request = self.paid_request(1000)

				self.refund_through_live_razorpay(
					payment_request,
					200,
					target="requests.Session.request",
					return_value=razorpay_response(200, body),
				)

				self.assert_reserved(payment_request, 200)

	def test_a_refund_razorpay_refuses_fails(self):
		payment_request = self.paid_request(1000)

		with self.assertRaises(GatewayRefusedError):
			self.refund_through_live_razorpay(
				payment_request,
				200,
				target="requests.Session.request",
				return_value=razorpay_error(400, "The refund amount exceeds the payment"),
			)

		refund = frappe.get_last_doc("Gateway Refund", {"payment_request": payment_request.name})
		self.assertEqual(refund.status, "Failed")
		self.assertEqual(payment_request.get_remaining_refundable_amount(), 1000)
