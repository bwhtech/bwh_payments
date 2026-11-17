# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.api import create_payment
from bwh_payments.bwh_payments import checkout
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	GATEWAY,
	configure_stripe_gateway,
	make_payment_request,
	remove_stripe_gateway,
)
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RazorpayTestCase,
)
from bwh_payments.services.razorpay.stub import StubRazorpayClient, sign_inline_payment
from bwh_payments.services.stripe.stub import StubStripeClient


class TestReturnFromGateway(IntegrationTestCase):
	def setUp(self):
		StubStripeClient.reset()
		configure_stripe_gateway()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		frappe.db.rollback()
		remove_stripe_gateway()
		frappe.db.commit()

	def restore_request(self):
		frappe.local.request = self.original_request
		frappe.set_user("Administrator")

	def return_as_guest(self, request_name: str):
		frappe.set_user("Guest")
		response = self.return_to_site(request_name)
		frappe.set_user("Administrator")
		return response

	def return_to_site(self, request_name: str):
		environ = EnvironBuilder(
			method="GET", path="/api/method/bwh_payments.bwh_payments.checkout.return_from_gateway"
		).get_environ()
		frappe.local.request = Request(environ)
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		checkout.return_from_gateway(request_name)
		return frappe.local.response

	def test_a_signed_in_shopper_keeps_their_session(self):
		payment_request = create_payment("Currency", "SAR", 100, "SAR", GATEWAY, redirect_to="/orders/1")
		StubStripeClient.pay(payment_request.order_ref)
		# The request saves this session back once it ends, so the shopper's sid and data must survive.
		session = frappe.local.session
		session.update({"sid": "shopper-sid", "data": frappe._dict(session_expiry="240:00:00")})

		self.return_to_site(payment_request.name)

		self.assertEqual((session.sid, session.data.session_expiry), ("shopper-sid", "240:00:00"))

	def test_the_shopper_lands_on_the_redirect_after_the_payment_is_confirmed(self):
		payment_request = create_payment(
			"Currency", "SAR", 100, "SAR", GATEWAY, redirect_to="/orders/1?token=x"
		)
		StubStripeClient.pay(payment_request.order_ref)

		response = self.return_as_guest(payment_request.name)

		self.assertEqual((response.type, response.location), ("redirect", "/orders/1?token=x"))
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_the_confirmed_status_is_kept_although_the_return_is_a_get_request(self):
		payment_request = create_payment("Currency", "SAR", 100, "SAR", GATEWAY, redirect_to="/orders/1")
		StubStripeClient.pay(payment_request.order_ref)
		frappe.local.flags.commit = False
		self.addCleanup(setattr, frappe.local.flags, "commit", False)

		self.return_as_guest(payment_request.name)

		# Frappe rolls back a GET request unless this flag asks it to commit (frappe.app.sync_database).
		self.assertTrue(frappe.local.flags.commit)

	def test_a_shopper_who_backed_out_still_lands_on_the_redirect_and_stays_pending(self):
		payment_request = create_payment("Currency", "SAR", 100, "SAR", GATEWAY, redirect_to="/orders/1")

		response = self.return_as_guest(payment_request.name)

		self.assertEqual(response.location, "/orders/1")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_payment_without_a_redirect_cannot_be_returned_to(self):
		payment_request = make_payment_request(100, "SAR")

		with self.assertRaises(frappe.DoesNotExistError):
			self.return_as_guest(payment_request.name)

	def test_an_unknown_payment_cannot_be_returned_to(self):
		with self.assertRaises(frappe.DoesNotExistError):
			self.return_as_guest("not-a-payment")

	def test_a_request_on_a_gateway_without_inline_checkout_is_refused(self):
		payment_request = create_payment("Currency", "SAR", 100, "SAR", GATEWAY)
		frappe.set_user("Guest")

		with self.assertRaisesRegex(frappe.PermissionError, "could not be verified"):
			checkout.confirm_inline_payment(payment_request.name, {})


class TestConfirmInlinePayment(RazorpayTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.set_user, "Administrator")

	def make_request(self, redirect_to: str = "/orders/1"):
		payment_request = create_payment(
			"Currency", "INR", 100, "INR", "Razorpay", inline_sdks=("razorpay",), redirect_to=redirect_to
		)
		self.payment_id = StubRazorpayClient.pay_order(payment_request.order_ref)
		return payment_request

	def sign(self, payment_request, secret: str = "rzp_test_secret") -> dict:
		return {
			"razorpay_order_id": payment_request.order_ref,
			"razorpay_payment_id": self.payment_id,
			"razorpay_signature": sign_inline_payment(payment_request.order_ref, self.payment_id, secret),
		}

	def test_a_guest_with_a_signed_response_marks_the_paid_order_paid_and_gets_the_redirect(self):
		payment_request = self.make_request("/orders/1?token=x")
		frappe.set_user("Guest")

		redirect_to = checkout.confirm_inline_payment(payment_request.name, self.sign(payment_request))

		frappe.set_user("Administrator")
		self.assertEqual(redirect_to, "/orders/1?token=x")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_bad_signature_is_refused_and_the_payment_stays_pending(self):
		payment_request = self.make_request()
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			checkout.confirm_inline_payment(
				payment_request.name, self.sign(payment_request, "attacker_guess")
			)

		frappe.set_user("Administrator")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unknown_request_is_refused_like_a_bad_signature(self):
		frappe.set_user("Guest")

		with self.assertRaisesRegex(frappe.PermissionError, "could not be verified"):
			checkout.confirm_inline_payment("not-a-payment", {})
