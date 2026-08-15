# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments.bwh_payments.doctype.stripe_gateway_settings import stripe_gateway_settings
from bwh_payments.currency import to_minor_units
from bwh_payments.tests.fake_stripe import FakeStripeClient

GATEWAY = "Stripe Test Gateway"
WEBHOOK_SECRET = "whsec_test_secret"

# None of these links are exercised here, and generating their fixtures drags in ERPNext's whole test
# bootstrap for no benefit.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_stripe_gateway():
	settings = frappe.get_single("Stripe Gateway Settings")
	settings.update(
		{
			"enabled": 1,
			"mode": "Test",
			"public_key": "pk_test_x",
			"private_key": "sk_test_x",
			"webhook_secret": WEBHOOK_SECRET,
			"success_url": "https://shop.test/en/account/orders/confirmation",
			"failure_url": "https://shop.test/en/cart/checkout",
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", GATEWAY):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": GATEWAY,
				"gateway_settings": "Stripe Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def make_payment_request(amount: float, currency: str = "SAR"):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": GATEWAY,
			"amount": amount,
			"currency_code": currency,
			"ref_doctype": "Currency",
			"ref_docname": currency,
		}
	).insert(ignore_permissions=True)


class TestGatewayPaymentRequest(IntegrationTestCase):
	def setUp(self):
		FakeStripeClient.reset()
		self.stripe_client_patch = patch.object(
			stripe_gateway_settings.stripe, "StripeClient", FakeStripeClient
		)
		self.stripe_client_patch.start()
		self.addCleanup(self.stripe_client_patch.stop)
		configure_stripe_gateway()

	def use_currency_number_format(self):
		"""Let Currency fields take their precision from the currency, not the site number format."""
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 1)
		frappe.clear_cache()

	def mark_paid(self, payment_request):
		FakeStripeClient.register_paid_session(
			payment_request.order_ref, payment_request.currency_code.lower()
		)
		payment_request.db_set("status", "Paid", update_modified=False)
		payment_request.reload()

	# --- session creation -------------------------------------------------

	def test_create_session_charges_iso_minor_units_for_a_three_decimal_currency(self):
		"""KWD has three decimals. The ported int(amount * 100) billed 1234 fils for 12.345 KWD."""
		self.use_currency_number_format()
		payment_request = make_payment_request(12.345, "KWD")

		charged = FakeStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.assertEqual(charged, 12345)
		self.assertNotEqual(charged, int(12.345 * 100))
		self.assertTrue(payment_request.order_ref)

	def test_a_three_decimal_amount_is_refused_when_the_site_stores_only_two(self):
		"""Better to refuse the charge than to store 12.35 KWD and bill the shopper a different figure."""
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 0)
		frappe.clear_cache()

		with self.assertRaises(frappe.ValidationError):
			make_payment_request(12.345, "KWD")

		self.assertEqual(FakeStripeClient.created_sessions, [])

	def test_create_session_charges_iso_minor_units_for_a_zero_decimal_currency(self):
		make_payment_request(1000, "JPY")

		charged = FakeStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.assertEqual(charged, 1000)
		self.assertNotEqual(charged, int(1000 * 100))

	def test_success_url_keeps_the_stripe_placeholder_and_existing_query(self):
		settings = frappe.get_single("Stripe Gateway Settings")
		settings.db_set("success_url", "https://shop.test/en/confirm?ref=abc", update_modified=False)
		settings.reload()

		success_url = settings.build_success_url()

		self.assertIn("ref=abc", success_url)
		self.assertIn("session_id={CHECKOUT_SESSION_ID}", success_url)
		self.assertEqual(success_url.count("?"), 1)

	# --- refund ledger ----------------------------------------------------

	def test_charge_and_refund_agree_on_the_minor_unit_conversion(self):
		self.use_currency_number_format()
		payment_request = make_payment_request(12.345, "KWD")
		charged = FakeStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.mark_paid(payment_request)

		payment_request.refund()

		self.assertEqual(FakeStripeClient.created_refunds[-1]["amount"], charged)
		self.assertEqual(to_minor_units(payment_request.refund_amount, "KWD"), charged)
		self.assertEqual(payment_request.status, "Refunded")
