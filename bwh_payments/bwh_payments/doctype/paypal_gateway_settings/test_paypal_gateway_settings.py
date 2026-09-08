# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.bwh_payments.doctype.paypal_gateway_settings import paypal_gateway_settings
from bwh_payments.tests.fake_paypal import FakePayPal, FakePayPalError

PAYPAL_GATEWAY = "PayPal"
PAYPAL_WEBHOOK_ID = "WH-TEST-WEBHOOK-ID"
SUCCESS_URL = "https://shop.test/en/account/orders/confirmation"

SETTINGS_FIELDS = (
	"enabled",
	"mode",
	"client_id",
	"client_secret",
	"webhook_id",
	"brand_name",
	"currency",
	"success_url",
	"cancelled_url",
	"failure_url",
)
# Read back through `get_password`, not off the document: a Password field holds a mask on the loaded doc
# and its real value lives in `__Auth`.
SETTINGS_PASSWORD_FIELDS = ("client_secret",)

# What this site had before the suite overwrote it. `create_request_log` commits, so every test that
# touches the transport escapes the per-test rollback and these writes stick — which means a run against
# a site with a real, working PayPal account configured would otherwise blank its credentials for good.
_original_state = {}

# None of these links are exercised here, and generating their fixtures drags in ERPNext's whole test
# bootstrap for no benefit.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_paypal_gateway():
	settings = frappe.get_single("PayPal Gateway Settings")
	remember_original_state(settings)
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


def remember_original_state(settings):
	"""Snapshot what the site had before the suite overwrites it, so teardown can put it back.

	Blanking unconditionally is the obvious wrong move: on a site where this gateway is genuinely
	configured it silently takes a working payment method off the storefront, and there is no undo.
	"""
	if _original_state:
		return

	# None when the profile does not exist at all, which is how teardown tells "the suite created this"
	# from "the site already had it".
	_original_state["profile_enabled"] = frappe.db.get_value(
		"Payment Gateway Profile", PAYPAL_GATEWAY, "enabled"
	)
	snapshot = {
		field: settings.get(field)
		for field in SETTINGS_FIELDS
		if field not in SETTINGS_PASSWORD_FIELDS
	}
	for field in SETTINGS_PASSWORD_FIELDS:
		snapshot[field] = settings.get_password(field, raise_exception=False)
	_original_state["settings"] = snapshot


def remove_paypal_gateway():
	"""Undo configure_paypal_gateway, restoring whatever the site had before rather than blanking it.

	The controller calls `create_request_log` on every HTTP call, and that helper ends in an unconditional
	`frappe.db.commit()`. So every test that touches the transport escapes the per-test rollback and pins
	its rows to the site. Without this the dev site is left with an enabled gateway backed by a fake client
	id, which the storefront then offers shoppers at checkout.
	"""
	for request_name in frappe.get_all(
		"Gateway Payment Request", filters={"gateway": PAYPAL_GATEWAY}, pluck="name"
	):
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	for service in ("PayPal", f"{PAYPAL_GATEWAY} Webhook", f"{PAYPAL_GATEWAY} Refund"):
		for log_name in frappe.get_all(
			"Integration Request", filters={"integration_request_service": service}, pluck="name"
		):
			frappe.delete_doc(
				"Integration Request", log_name, ignore_permissions=True, delete_permanently=True
			)

	restore_paypal_profile()
	restore_paypal_settings()
	frappe.clear_cache(doctype="PayPal Gateway Settings")


def restore_paypal_profile():
	original_enabled = _original_state.get("profile_enabled")
	if original_enabled is None:
		frappe.delete_doc(
			"Payment Gateway Profile",
			PAYPAL_GATEWAY,
			ignore_missing=True,
			ignore_permissions=True,
			force=True,
		)
		return

	# The site had this profile before the suite ran, so its switch goes back rather than the row going away.
	frappe.db.set_value("Payment Gateway Profile", PAYPAL_GATEWAY, "enabled", original_enabled)


def restore_paypal_settings():
	original = _original_state.get("settings") or {}
	if not original.get("client_id"):
		# Nothing was configured here before. set_single_value, not save(): the credentials are mandatory,
		# so a blank state cannot go through validation.
		frappe.db.set_single_value(
			"PayPal Gateway Settings",
			{field: "" for field in SETTINGS_FIELDS} | {"enabled": 0},
		)
		return

	# A real account was configured before the suite overwrote it. Going through the ORM is what puts a
	# Password field back into `__Auth`; set_single_value would leave the secret in plain text in
	# `tabSingles` and the gateway unable to authenticate.
	settings = frappe.get_single("PayPal Gateway Settings")
	settings.update(original)
	settings.save(ignore_permissions=True)


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
		FakePayPal.reset()
		# Raw HTTP, no SDK: the seam is the two request helpers imported into the controller's namespace.
		for helper, replacement in (
			("make_post_request", FakePayPal.post),
			("make_get_request", FakePayPal.get),
		):
			transport_patch = patch.object(paypal_gateway_settings, helper, replacement)
			transport_patch.start()
			self.addCleanup(transport_patch.stop)
		configure_paypal_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Roll back the suite's own writes first so the commit below persists nothing but the cleanup, and
		# commit it because the class-level rollback frappe queues after this would otherwise undo it.
		frappe.db.rollback()
		remove_paypal_gateway()
		frappe.db.commit()

	def get_settings(self):
		return frappe.get_single("PayPal Gateway Settings")


class TestPayPalGatewaySettings(PayPalTestCase):
	# --- session creation -------------------------------------------------

	def test_charges_major_units_as_a_decimal_string(self):
		make_paypal_payment_request(12.5, "USD")

		amount = FakePayPal.created_orders[0]["purchase_units"][0]["amount"]
		# PayPal reads "12.5" and "12.50" the same way, but it rejects a bare float outright, and the
		# string is what a shopper is shown on the approval page.
		self.assertEqual(amount, {"currency_code": "USD", "value": "12.50"})

	def test_charges_a_zero_decimal_currency_without_decimals(self):
		make_paypal_payment_request(1000, "JPY")

		self.assertEqual(FakePayPal.created_orders[0]["purchase_units"][0]["amount"]["value"], "1000")

	def test_refuses_a_fractional_amount_in_a_currency_paypal_bills_whole(self):
		# HUF is two-decimal under ISO 4217 but whole-only at PayPal, so rounding here would bill the
		# shopper a different figure than the one they agreed to.
		with self.assertRaises(frappe.ValidationError):
			paypal_gateway_settings.format_paypal_amount(1000.50, "HUF")

	def test_refuses_a_currency_paypal_cannot_settle(self):
		with self.assertRaises(frappe.ValidationError):
			make_paypal_payment_request(100, "SAR")

	def test_session_carries_the_order_id_and_approval_link(self):
		payment_request = make_paypal_payment_request(20, "USD")

		self.assertIn(payment_request.order_ref, FakePayPal.orders)
		# The order id, not a capture id: webhook.handle matches order_ref byte-for-byte.
		self.assertEqual(FakePayPal.orders[payment_request.order_ref]["id"], payment_request.order_ref)
		self.assertIn(payment_request.order_ref, payment_request.order_url)

	def test_success_url_carries_our_reference_alongside_an_existing_query(self):
		settings = self.get_settings()
		settings.success_url = f"{SUCCESS_URL}?utm_source=paypal"
		settings.save(ignore_permissions=True)

		url = settings.build_return_url(settings.success_url, "Success URL", "GPR-0001")

		self.assertIn("utm_source=paypal", url)
		self.assertIn("reference_id=GPR-0001", url)
		# String concatenation would have produced a second "?" and broken both parameters.
		self.assertEqual(url.count("?"), 1)

	def test_missing_success_url_is_refused_rather_than_sent_blank(self):
		settings = self.get_settings()
		with self.assertRaises(frappe.ValidationError):
			settings.build_return_url("", "Success URL", "GPR-0001")

	# --- access token -----------------------------------------------------

	def test_access_token_is_fetched_once_and_reused(self):
		settings = self.get_settings()
		settings.get_access_token()
		settings.get_access_token()

		# A token per call would double every round trip the shopper waits on.
		self.assertEqual(len(FakePayPal.token_requests), 1)
		self.assertEqual(FakePayPal.token_requests[0], "grant_type=client_credentials")

	# --- polled status ----------------------------------------------------

	def test_status_map_covers_the_terminal_states(self):
		settings = self.get_settings()
		for paypal_status, expected in (
			("COMPLETED", "Paid"),
			("VOIDED", "Cancelled"),
			("CREATED", "Pending"),
			("PAYER_ACTION_REQUIRED", "Pending"),
		):
			with self.subTest(paypal_status=paypal_status):
				order_id = FakePayPal.register_order(status=paypal_status)
				self.assertEqual(settings.get_payment_status(order_id), expected)

	def test_an_unmapped_status_stays_pending(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="SOMETHING_NEW")

		# Never terminal and never Paid, so an unmapped PayPal state can neither release goods nor cancel
		# a live order.
		self.assertEqual(settings.get_payment_status(order_id), "Pending")

	def test_an_approved_order_is_captured_before_it_is_called_paid(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="APPROVED")

		status = settings.get_payment_status(order_id)

		# Approval is permission, not payment. Without the capture the shopper is told they paid and no
		# money ever arrives.
		self.assertEqual(FakePayPal.captured_orders, [order_id])
		self.assertEqual(status, "Paid")

	def test_losing_the_capture_race_still_reports_paid(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="APPROVED")
		# Stand in for the confirmation page and the CHECKOUT.ORDER.APPROVED webhook both capturing: the
		# loser is answered ORDER_ALREADY_CAPTURED, which means the other call took the money.
		FakePayPal.add_capture(order_id)
		FakePayPal.orders[order_id]["status"] = "COMPLETED"
		FakePayPal.next_capture_already_captured = True

		self.assertEqual(settings.get_payment_status(order_id), "Paid")
		self.assertEqual(FakePayPal.captured_orders, [])

	def test_a_capture_that_really_failed_raises_instead_of_reporting_paid(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="APPROVED")
		declined = FakePayPalError(
			{"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]}
		)

		# Only ORDER_ALREADY_CAPTURED means someone else took the money. Every other refusal has to
		# propagate, leaving the request Pending rather than reporting a charge that never happened.
		with patch.object(FakePayPal, "capture", side_effect=declined):
			with self.assertRaises(frappe.ValidationError):
				settings.get_payment_status(order_id)

	# --- refunds ----------------------------------------------------------

	def test_refund_goes_against_the_completed_capture(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="COMPLETED")
		FakePayPal.add_capture(order_id, status="DECLINED")
		capture_id = FakePayPal.add_capture(order_id, status="COMPLETED")

		result = settings.refund_payment(order_id, 12.5, "USD")

		# An order is not a charge; refunding the order id, or a declined capture, is the obvious wrong move.
		self.assertEqual(FakePayPal.created_refunds[0]["capture_id"], capture_id)
		self.assertEqual(FakePayPal.created_refunds[0]["amount"]["value"], "12.50")
		self.assertEqual(result["status"], "COMPLETED")
		self.assertEqual(result["amount"], 12.5)

	def test_refund_without_a_capture_is_refused_with_something_actionable(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="APPROVED")

		with self.assertRaises(frappe.ValidationError):
			settings.refund_payment(order_id, 5, "USD")
		self.assertEqual(FakePayPal.created_refunds, [])

	def test_a_rejected_refund_raises_rather_than_reporting_success(self):
		settings = self.get_settings()
		order_id = FakePayPal.register_order(status="COMPLETED")
		FakePayPal.add_capture(order_id)
		FakePayPal.next_refund_status = "FAILED"

		with self.assertRaises(frappe.ValidationError):
			settings.refund_payment(order_id, 5, "USD")

	# --- contract ---------------------------------------------------------

	def test_declares_the_currencies_it_can_settle(self):
		supported = self.get_settings().get_supported_currencies()

		self.assertIn("USD", supported)
		# The whole reason the contract grew this method: a Gulf storefront must not offer PayPal on its
		# home currency.
		self.assertNotIn("SAR", supported)
