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
		environ = EnvironBuilder(
			method="GET", path="/api/method/bwh_payments.bwh_payments.checkout.return_from_gateway"
		).get_environ()
		frappe.local.request = Request(environ)
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		frappe.set_user("Guest")
		checkout.return_from_gateway(request_name)
		frappe.set_user("Administrator")
		return frappe.local.response

	def test_the_shopper_lands_on_the_redirect_after_the_payment_is_confirmed(self):
		payment_request = create_payment(
			"Currency", "SAR", 100, "SAR", GATEWAY, redirect_to="/orders/1?token=x"
		)
		StubStripeClient.pay(payment_request.order_ref)

		response = self.return_as_guest(payment_request.name)

		self.assertEqual((response.type, response.location), ("redirect", "/orders/1?token=x"))
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

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
