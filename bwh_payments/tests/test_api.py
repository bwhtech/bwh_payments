# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.geo.doctype.currency.currency import Currency
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments.api import (
	get_gateway_options,
	get_gateways,
	record_external_payment,
	record_external_refund,
	refund,
	sync_refunds,
	validate_currency,
)
from bwh_payments.base_class import PaymentGatewayBase
from bwh_payments.bwh_payments import utils
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RazorpayTestCase,
	configure_razorpay_gateway,
	make_razorpay_payment_request,
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


class TestGateways(IntegrationTestCase):
	def get_gateway(self, name: str):
		return next(gateway for gateway in get_gateways() if gateway.name == name)

	def test_every_installed_gateway_is_listed_whether_or_not_it_is_set_up(self):
		names = [gateway.name for gateway in get_gateways()]

		self.assertTrue({"Razorpay", "Stripe", "Tabby", "Telr"} <= set(names))
		self.assertEqual(names, sorted(names, key=str.casefold))

	def test_a_gateway_whose_code_is_missing_is_skipped(self):
		get_controller = utils.get_controller

		def get_controller_without_stripe(doctype: str):
			if doctype == "Stripe Gateway Settings":
				raise ImportError("No module named stripe_gateway_settings")
			return get_controller(doctype)

		with patch.object(utils, "get_controller", side_effect=get_controller_without_stripe):
			names = [gateway.name for gateway in get_gateways()]

		self.assertNotIn("Stripe", names)
		self.assertIn("Razorpay", names)

	def test_each_gateway_names_the_settings_behind_it(self):
		for gateway in get_gateways():
			with self.subTest(gateway=gateway.name):
				settings = frappe.get_single(gateway.settings_doctype)
				self.assertIsInstance(settings, PaymentGatewayBase)
				self.assertEqual(gateway.name, settings.get_gateway_name())

	def test_a_gateway_carries_what_a_merchant_needs_to_choose_and_set_it_up(self):
		gateway = self.get_gateway("Razorpay")

		self.assertTrue(gateway.description)
		self.assertTrue(gateway.docs_url.startswith("https://"))
		self.assertTrue(gateway.logo.startswith("/assets/bwh_payments/images/gateways/razorpay"))
		self.assertFalse(gateway.reusable_checkout)


class TestRefundsAndImports(RazorpayTestCase):
	def import_paid_payment(self, order_ref: str = "pay_legacy_1"):
		return record_external_payment(
			"Currency", "INR", 100, "INR", RAZORPAY_GATEWAY, "Paid", order_ref, transaction_ref=order_ref
		)

	def test_refund_and_sync_work_by_request_name(self):
		payment_request = make_razorpay_payment_request(100)
		StubRazorpayClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		StubRazorpayClient.next_refund_status = "pending"

		gateway_refund = refund(payment_request.name, 40, reference="MY-REFUND-1")
		StubRazorpayClient.set_refund_status(gateway_refund.refund_id, "processed")
		sync_refunds(payment_request.name)

		gateway_refund.reload()
		self.assertEqual((gateway_refund.status, gateway_refund.reference), ("Processed", "MY-REFUND-1"))

	def test_an_imported_payment_opens_no_checkout_and_settles_nothing(self):
		with patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized:
			payment_request = self.import_paid_payment()

		on_payment_authorized.assert_not_called()
		self.assertEqual(StubRazorpayClient.created_links, [])
		self.assertEqual(
			(payment_request.status, payment_request.order_ref, payment_request.gateway_transaction_ref),
			("Paid", "pay_legacy_1", "pay_legacy_1"),
		)

	def test_importing_the_same_payment_twice_keeps_one_record(self):
		first = self.import_paid_payment("pay_legacy_twice")
		second = self.import_paid_payment("pay_legacy_twice")

		self.assertEqual(first.name, second.name)
		self.assertEqual(frappe.db.count("Gateway Payment Request", {"order_ref": "pay_legacy_twice"}), 1)

	def test_an_imported_refund_counts_against_the_payment_without_telling_the_document(self):
		payment_request = self.import_paid_payment("pay_legacy_refunded")

		with patch.object(Currency, "on_refund_update", create=True) as on_refund_update:
			record_external_refund(
				payment_request.name, "rfnd_legacy_1", "Processed", 30, reference="OLD-REF-1"
			)
			record_external_refund(
				payment_request.name, "rfnd_legacy_1", "Processed", 30, reference="OLD-REF-1"
			)

		on_refund_update.assert_not_called()
		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 30)
		self.assertEqual(payment_request.status, "Partially Refunded")
		self.assertEqual(payment_request.get_remaining_refundable_amount(), 70)

	def test_an_imported_razorpay_payment_can_be_refunded_against_its_payment_id(self):
		payment_request = self.import_paid_payment("pay_legacy_refundable")

		refund(payment_request.name, 70)

		self.assertEqual(StubRazorpayClient.created_refunds[-1]["payment_id"], "pay_legacy_refundable")
