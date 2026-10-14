# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.api import get_gateways
from bwh_payments.bwh_payments.doctype.paypal_gateway_settings import paypal_gateway_settings
from bwh_payments.services.paypal.stub import StubPayPalClient

PAYPAL_GATEWAY = "PayPal"
PAYPAL_WEBHOOK_ID = "WH-TEST-WEBHOOK-ID"
SUCCESS_URL = "https://shop.test/en/account/orders/confirmation"

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_paypal_gateway():
	settings = frappe.get_single("PayPal Gateway Settings")
	settings.update(
		{
			"enabled": 1,
			"mode": "Sandbox",
			"client_id": "paypal_test_client_id",
			"client_secret": "paypal_test_client_secret",
			"webhook_id": PAYPAL_WEBHOOK_ID,
			"brand_name": "Test Shop",
			"currency": "USD",
			"success_url": SUCCESS_URL,
			"cancelled_url": "https://shop.test/en/cart",
			"failure_url": "https://shop.test/en/cart/checkout",
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", PAYPAL_GATEWAY):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": PAYPAL_GATEWAY,
				"gateway_settings": "PayPal Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def remove_paypal_gateway():
	"""`create_request_log` commits, so these tests escape the rollback and need this cleanup."""
	frappe.db.delete(
		"Gateway Refund",
		{
			"payment_request": [
				"in",
				frappe.get_all("Gateway Payment Request", {"gateway": PAYPAL_GATEWAY}, pluck="name"),
			]
		},
	)
	for request_name in frappe.get_all(
		"Gateway Payment Request", filters={"gateway": PAYPAL_GATEWAY}, pluck="name"
	):
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	for service in ("PayPal", f"{PAYPAL_GATEWAY} Webhook"):
		for log_name in frappe.get_all(
			"Integration Request", filters={"integration_request_service": service}, pluck="name"
		):
			frappe.delete_doc(
				"Integration Request", log_name, ignore_permissions=True, delete_permanently=True
			)

	frappe.delete_doc(
		"Payment Gateway Profile",
		PAYPAL_GATEWAY,
		ignore_missing=True,
		ignore_permissions=True,
		force=True,
	)
	# Not save(): the credentials are mandatory.
	frappe.db.set_single_value(
		"PayPal Gateway Settings",
		{
			"enabled": 0,
			"client_id": "",
			"client_secret": "",
			"webhook_id": "",
			"brand_name": "",
			"currency": "",
			"success_url": "",
			"cancelled_url": "",
			"failure_url": "",
		},
	)
	frappe.clear_cache(doctype="PayPal Gateway Settings")


def make_paypal_payment_request(amount: float, currency: str = "USD"):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": PAYPAL_GATEWAY,
			"amount": amount,
			"currency_code": currency,
			"ref_doctype": "Currency",
			"ref_docname": currency,
		}
	).insert(ignore_permissions=True)


class PayPalTestCase(IntegrationTestCase):
	def setUp(self):
		StubPayPalClient.reset()
		configure_paypal_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Commit only the cleanup; frappe's class-level rollback would undo it otherwise.
		frappe.db.rollback()
		remove_paypal_gateway()
		frappe.db.commit()  # nosemgrep

	def get_settings(self):
		return frappe.get_single("PayPal Gateway Settings")


class TestPayPalGatewaySettings(PayPalTestCase):
	def test_charges_major_units_as_a_decimal_string(self):
		make_paypal_payment_request(12.5, "USD")

		amount = StubPayPalClient.created_orders[0]["purchase_units"][0]["amount"]
		self.assertEqual(amount, {"currency_code": "USD", "value": "12.50"})

	def test_charges_a_zero_decimal_currency_without_decimals(self):
		make_paypal_payment_request(1000, "JPY")

		self.assertEqual(StubPayPalClient.created_orders[0]["purchase_units"][0]["amount"]["value"], "1000")

	def test_refuses_a_fractional_amount_in_a_currency_paypal_bills_whole(self):
		with self.assertRaises(frappe.ValidationError):
			paypal_gateway_settings.format_paypal_amount(1000.50, "HUF")

	def test_refuses_a_currency_paypal_cannot_settle(self):
		with self.assertRaises(frappe.ValidationError):
			make_paypal_payment_request(100, "SAR")
		self.assertEqual(StubPayPalClient.created_orders, [])

	def test_session_carries_the_order_id_and_approval_link(self):
		payment_request = make_paypal_payment_request(20, "USD")

		self.assertIn(payment_request.order_ref, StubPayPalClient.orders)
		self.assertIn(payment_request.order_ref, payment_request.order_url)
		self.assertTrue(payment_request.order_url.startswith("https://paypal.test/checkoutnow"))

	def test_success_url_carries_our_reference_alongside_an_existing_query(self):
		settings = self.get_settings()
		settings.success_url = f"{SUCCESS_URL}?utm_source=paypal"
		settings.save(ignore_permissions=True)

		url = settings.build_return_url(settings.success_url, "Success URL", "GPR-0001")

		self.assertIn("utm_source=paypal", url)
		self.assertIn("reference_id=GPR-0001", url)
		self.assertEqual(url.count("?"), 1)

	def test_missing_success_url_is_refused_rather_than_sent_blank(self):
		settings = self.get_settings()
		with self.assertRaises(frappe.ValidationError):
			settings.build_return_url("", "Success URL", "GPR-0001")

	def test_status_map_covers_the_terminal_states(self):
		settings = self.get_settings()
		for paypal_status, expected in (
			("COMPLETED", "Paid"),
			("VOIDED", "Cancelled"),
			("CREATED", "Pending"),
			("PAYER_ACTION_REQUIRED", "Pending"),
		):
			with self.subTest(paypal_status=paypal_status):
				order_id = StubPayPalClient.add_order(status=paypal_status)
				self.assertEqual(settings.get_payment_status(order_id), expected)

	def test_an_unmapped_status_stays_pending(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order(status="SOMETHING_NEW")

		self.assertEqual(settings.get_payment_status(order_id), "Pending")

	def test_an_approved_order_is_captured_before_it_is_called_paid(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.approve(order_id)

		status = settings.get_payment_status(order_id)

		self.assertEqual(StubPayPalClient.captured_orders, [order_id])
		self.assertEqual(status, "Paid")

	def test_losing_the_capture_race_still_reports_paid(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.approve(order_id)
		StubPayPalClient.lose_next_capture_race = True

		self.assertEqual(settings.get_payment_status(order_id), "Paid")
		self.assertEqual(StubPayPalClient.captured_orders, [])

	def test_a_capture_that_really_failed_raises_instead_of_reporting_paid(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.approve(order_id)
		StubPayPalClient.next_capture_issue = "INSTRUMENT_DECLINED"

		with self.assertRaises(frappe.ValidationError):
			settings.get_payment_status(order_id)
		self.assertEqual(StubPayPalClient.captured_orders, [])

	def test_refund_goes_against_the_completed_capture(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order(status="COMPLETED")
		StubPayPalClient.add_capture(order_id, status="DECLINED")
		capture_id = StubPayPalClient.add_capture(order_id, status="COMPLETED")

		result = settings.refund_payment(order_id, 12.5, "USD")

		self.assertEqual(StubPayPalClient.refunds[0]["capture_id"], capture_id)
		self.assertEqual(StubPayPalClient.refunds[0]["amount"]["value"], "12.50")
		# In the words Gateway Refund understands, so a completed refund is recorded as Processed.
		self.assertEqual(result.status, "processed")
		self.assertEqual(result.amount, 12.5)

	def test_a_partially_refunded_capture_can_refund_its_balance(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order(status="COMPLETED")
		capture_id = StubPayPalClient.add_capture(order_id, status="PARTIALLY_REFUNDED")

		settings.refund_payment(order_id, 5, "USD")

		self.assertEqual(StubPayPalClient.refunds[0]["capture_id"], capture_id)

	def test_refund_without_a_capture_is_refused_with_something_actionable(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.approve(order_id)

		with self.assertRaises(frappe.ValidationError):
			settings.refund_payment(order_id, 5, "USD")
		self.assertEqual(StubPayPalClient.refunds, [])

	def test_a_rejected_refund_raises_rather_than_reporting_success(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.pay(order_id)
		StubPayPalClient.next_refund_status = "FAILED"

		with self.assertRaises(frappe.ValidationError):
			settings.refund_payment(order_id, 5, "USD")

	def test_refund_of_an_unknown_order_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings().refund_payment("PAYPALNOTHING", 5, "USD")
		self.assertEqual(StubPayPalClient.refunds, [])

	def test_release_if_unpaid_keeps_an_open_order_pending(self):
		payment_request = make_paypal_payment_request(20, "USD")

		# PayPal has no cancel endpoint for an unapproved order, so the request stays open.
		self.assertFalse(payment_request.release_if_unpaid())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_release_if_unpaid_refuses_a_paid_order(self):
		payment_request = make_paypal_payment_request(20, "USD")
		StubPayPalClient.pay(payment_request.order_ref)

		self.assertFalse(payment_request.release_if_unpaid())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_declares_the_currencies_it_can_settle(self):
		supported = self.get_settings().supported_currencies()

		self.assertIn("USD", supported)
		self.assertNotIn("SAR", supported)

	def test_a_pending_refund_settles_when_paypal_completes_it(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		StubPayPalClient.pay(order_id)
		StubPayPalClient.next_refund_status = "PENDING"

		result = settings.refund_payment(order_id, 5, "USD")
		self.assertEqual(result.status, "PENDING")

		StubPayPalClient.set_refund_status(result.refund_id, "COMPLETED")
		self.assertEqual(settings.get_refund_status(order_id, result.refund_id).status, "processed")

	def test_a_refund_through_the_request_is_recorded_as_processed(self):
		payment_request = make_paypal_payment_request(20, "USD")
		StubPayPalClient.pay(payment_request.order_ref)
		payment_request.sync_status()

		refund = payment_request.create_refund(5)

		self.assertEqual(refund.status, "Processed")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_the_transaction_ref_is_the_completed_capture(self):
		settings = self.get_settings()
		order_id = StubPayPalClient.add_order()
		capture_id = StubPayPalClient.pay(order_id)

		self.assertEqual(settings.get_transaction_ref(order_id), capture_id)

	def test_a_fully_refunded_capture_is_still_the_transaction_ref(self):
		order_id = StubPayPalClient.add_order(status="COMPLETED")
		capture_id = StubPayPalClient.add_capture(order_id, status="REFUNDED")

		self.assertEqual(self.get_settings().get_transaction_ref(order_id), capture_id)

	def test_an_unpaid_order_has_no_transaction_ref(self):
		order_id = StubPayPalClient.add_order()

		self.assertIsNone(self.get_settings().get_transaction_ref(order_id))

	def test_a_return_url_replaces_every_way_back(self):
		session = self.get_settings().create_session(
			20, "USD", reference="GPR-0001", return_url="https://shop.test/return"
		)

		self.assertEqual(session.success_url, "https://shop.test/return")
		self.assertEqual(session.cancel_url, "https://shop.test/return")
		self.assertEqual(session.failure_url, "https://shop.test/return")
		context = StubPayPalClient.created_orders[0]["payment_source"]["paypal"]["experience_context"]
		self.assertEqual(context["return_url"], "https://shop.test/return")

	def test_paypal_is_listed_with_a_reusable_checkout(self):
		gateway = next(gateway for gateway in get_gateways() if gateway.name == PAYPAL_GATEWAY)

		self.assertEqual(gateway.settings_doctype, "PayPal Gateway Settings")
		self.assertTrue(gateway.reusable_checkout)
