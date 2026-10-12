# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe

from bwh_payments.api import get_gateway_options, validate_currency
from bwh_payments.bwh_payments.doctype.paypal_gateway_settings.test_paypal_gateway_settings import (
	PAYPAL_GATEWAY,
	PayPalTestCase,
)


class TestPayPalCurrencyFiltering(PayPalTestCase):
	def get_offered(self, currency: str) -> list[str]:
		return [option.name for option in get_gateway_options(currency)]

	def test_paypal_is_hidden_from_a_currency_it_cannot_settle(self):
		self.assertNotIn(PAYPAL_GATEWAY, self.get_offered("SAR"))
		self.assertIn(PAYPAL_GATEWAY, self.get_offered("USD"))

	def test_currency_matching_ignores_case_and_padding(self):
		self.assertIn(PAYPAL_GATEWAY, self.get_offered(" usd "))

	def test_validate_currency_refuses_what_paypal_cannot_take(self):
		with self.assertRaises(frappe.ValidationError):
			validate_currency(PAYPAL_GATEWAY, "SAR")
		validate_currency(PAYPAL_GATEWAY, "USD")
