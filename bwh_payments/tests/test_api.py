# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.api import get_gateway_options, validate_currency
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	configure_razorpay_gateway,
	remove_razorpay_gateway,
)
from bwh_payments.services.razorpay.stub import StubRazorpayClient

TABBY_GATEWAY = "Tabby API Test"


class TestGatewayOptions(IntegrationTestCase):
	def setUp(self):
		StubRazorpayClient.reset()
		configure_razorpay_gateway()
		# IntegrationTestCase rolls back only at class end, so a test that disabled it leaves it off.
		frappe.delete_doc("Payment Gateway Profile", TABBY_GATEWAY, ignore_missing=True, force=True)
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": TABBY_GATEWAY,
				"gateway_settings": "Tabby Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		frappe.db.rollback()
		remove_razorpay_gateway()
		frappe.db.commit()

	def get_option(self, name: str, currency: str | None = None):
		return next((option for option in get_gateway_options(currency) if option.name == name), None)

	def test_an_option_carries_the_gateways_logo_for_light_and_dark_themes(self):
		option = self.get_option(RAZORPAY_GATEWAY)

		self.assertEqual(option.title, RAZORPAY_GATEWAY)
		self.assertTrue(option.logo.startswith("/assets/bwh_payments/images/gateways/razorpay"))
		self.assertTrue(option.logo_dark)

	def test_a_gateway_that_cannot_take_the_currency_is_not_offered(self):
		self.assertIsNone(self.get_option(TABBY_GATEWAY, "INR"))
		self.assertIsNotNone(self.get_option(TABBY_GATEWAY, "SAR"))
		self.assertIsNotNone(self.get_option(RAZORPAY_GATEWAY, "INR"))

	def test_a_disabled_gateway_is_not_offered(self):
		frappe.db.set_value("Payment Gateway Profile", TABBY_GATEWAY, "enabled", 0)
		frappe.get_doc("Payment Gateway Profile", TABBY_GATEWAY).save(ignore_permissions=True)

		self.assertIsNone(self.get_option(TABBY_GATEWAY))

	def test_validate_currency_refuses_what_the_gateway_cannot_take(self):
		validate_currency(TABBY_GATEWAY, "SAR")

		with self.assertRaises(frappe.ValidationError):
			validate_currency(TABBY_GATEWAY, "INR")

	def test_validate_currency_refuses_a_currency_the_site_does_not_have(self):
		with self.assertRaises(frappe.ValidationError):
			validate_currency(RAZORPAY_GATEWAY, "XYZ")
