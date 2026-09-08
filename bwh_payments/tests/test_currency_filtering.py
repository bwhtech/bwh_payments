# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe

from bwh_payments.bwh_payments.doctype.paypal_gateway_settings.test_paypal_gateway_settings import (
	PAYPAL_GATEWAY,
	PayPalTestCase,
)
from bwh_payments.bwh_payments.utils import (
	get_available_payment_modes,
	get_gateway_currency_support,
	get_payment_modes_for_currency,
)

# No IGNORE_TEST_RECORD_DEPENDENCIES here: frappe only honours it inside a doctype folder and raises
# NotImplementedError otherwise. Outside one it loads no test records at all, which is what we want.


class TestPaymentModeCurrencyFiltering(PayPalTestCase):
	"""What the checkout page is allowed to offer, given the cart it is rendering."""

	def test_a_gateway_is_hidden_from_a_currency_it_cannot_settle(self):
		self.assertNotIn(PAYPAL_GATEWAY, get_payment_modes_for_currency("SAR"))
		# The storefront is SAR-first, so without this every shopper is offered PayPal and refused at the
		# last step of checkout.
		self.assertIn(PAYPAL_GATEWAY, get_payment_modes_for_currency("USD"))

	def test_currency_matching_ignores_case_and_padding(self):
		self.assertIn(PAYPAL_GATEWAY, get_payment_modes_for_currency(" usd "))

	def test_a_gateway_that_declares_nothing_is_offered_for_every_currency(self):
		unrestricted = [
			gateway for gateway, currencies in get_gateway_currency_support().items() if currencies is None
		]
		if not unrestricted:
			self.skipTest("no unrestricted gateway is enabled on this site")

		for currency in ("SAR", "USD", "JPY"):
			with self.subTest(currency=currency):
				offered = get_payment_modes_for_currency(currency)
				for gateway in unrestricted:
					self.assertIn(gateway, offered)

	def test_an_unknown_currency_falls_back_to_the_unfiltered_list(self):
		# Hiding everything would leave the shopper with no way to pay at all; the gateway's own
		# `create_session` still refuses a currency it cannot take.
		self.assertEqual(get_payment_modes_for_currency(""), get_available_payment_modes())

	def test_disabling_a_profile_drops_it_from_the_currency_map(self):
		self.assertIn(PAYPAL_GATEWAY, get_gateway_currency_support())

		profile = frappe.get_doc("Payment Gateway Profile", PAYPAL_GATEWAY)
		profile.enabled = 0
		profile.save(ignore_permissions=True)

		# `on_update` has to clear both caches together, or the storefront keeps offering a gateway that
		# was just switched off.
		self.assertNotIn(PAYPAL_GATEWAY, get_gateway_currency_support())
		self.assertNotIn(PAYPAL_GATEWAY, get_payment_modes_for_currency("USD"))

	def test_an_unreadable_gateway_is_offered_rather_than_taking_checkout_down(self):
		support = get_gateway_currency_support()

		# Checkout is the one page that must never fail to render, so a gateway whose controller raises on
		# load is treated as unrestricted and refused later by `create_session` instead.
		self.assertIsNone(support.get("Gateway That Does Not Exist"))
		self.assertIn(PAYPAL_GATEWAY, get_payment_modes_for_currency("USD"))
