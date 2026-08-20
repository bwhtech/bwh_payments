# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings import razorpay_gateway_settings
from bwh_payments.tests.fake_razorpay import (
	FakeRazorpay,
	build_payment_link_paid_event,
	sign_razorpay_payload,
)

RAZORPAY_GATEWAY = "Razorpay"
RAZORPAY_WEBHOOK_SECRET = "rzp_whsec_test_secret"
SUCCESS_URL = "https://shop.test/en/account/orders/confirmation"

# None of these links are exercised here, and generating their fixtures drags in ERPNext's whole test
# bootstrap for no benefit.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_razorpay_gateway():
	settings = frappe.get_single("Razorpay Gateway Settings")
	settings.update(
		{
			"enabled": 1,
			"test_mode": 1,
			"treat_authorised_as_paid": 0,
			"key_id": "rzp_test_x",
			"key_secret": "rzp_test_secret",
			"webhook_secret": RAZORPAY_WEBHOOK_SECRET,
			"currency": "INR",
			"success_url": SUCCESS_URL,
			"failure_url": "https://shop.test/en/cart/checkout",
			"cancelled_url": "https://shop.test/en/cart",
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", RAZORPAY_GATEWAY):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": RAZORPAY_GATEWAY,
				"gateway_settings": "Razorpay Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def remove_razorpay_gateway():
	"""Undo configure_razorpay_gateway.

	This matters more here than it does for Stripe: the Razorpay controller calls `create_request_log` on
	every single HTTP call, and that helper ends in an unconditional `frappe.db.commit()`. So every test
	that touches the transport escapes the per-test rollback and pins its rows to the site. Without this
	the dev site is left with an enabled gateway backed by a fake `rzp_test_x` key, which the storefront
	then offers shoppers at checkout.
	"""
	for request_name in frappe.get_all(
		"Gateway Payment Request", filters={"gateway": RAZORPAY_GATEWAY}, pluck="name"
	):
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	for service in ("Razorpay", f"{RAZORPAY_GATEWAY} Webhook"):
		for log_name in frappe.get_all(
			"Integration Request", filters={"integration_request_service": service}, pluck="name"
		):
			frappe.delete_doc(
				"Integration Request", log_name, ignore_permissions=True, delete_permanently=True
			)

	frappe.delete_doc(
		"Payment Gateway Profile",
		RAZORPAY_GATEWAY,
		ignore_missing=True,
		ignore_permissions=True,
		force=True,
	)
	# set_single_value, not save(): the keys are mandatory, so blanking them cannot go through validation.
	frappe.db.set_single_value(
		"Razorpay Gateway Settings",
		{
			"enabled": 0,
			"key_id": "",
			"key_secret": "",
			"webhook_secret": "",
			"currency": "",
			"success_url": "",
			"failure_url": "",
			"cancelled_url": "",
		},
	)
	frappe.clear_cache(doctype="Razorpay Gateway Settings")


def make_razorpay_payment_request(amount: float, currency: str = "INR"):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": RAZORPAY_GATEWAY,
			"amount": amount,
			"currency_code": currency,
			"ref_doctype": "Currency",
			"ref_docname": currency,
		}
	).insert(ignore_permissions=True)


class RazorpayTestCase(IntegrationTestCase):
	def setUp(self):
		FakeRazorpay.reset()
		# Raw HTTP, no SDK: the seam is the two request helpers imported into the controller's namespace.
		for helper, replacement in (
			("make_post_request", FakeRazorpay.post),
			("make_get_request", FakeRazorpay.get),
		):
			transport_patch = patch.object(razorpay_gateway_settings, helper, replacement)
			transport_patch.start()
			self.addCleanup(transport_patch.stop)
		configure_razorpay_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Roll back the suite's own writes first so the commit below persists nothing but the cleanup, and
		# commit it because the class-level rollback frappe queues after this would otherwise undo it.
		frappe.db.rollback()
		remove_razorpay_gateway()
		frappe.db.commit()

	def get_settings(self):
		return frappe.get_single("Razorpay Gateway Settings")


class TestRazorpayGatewaySettings(RazorpayTestCase):
	def use_currency_number_format(self):
		"""Let Currency fields take their precision from the currency, not the site number format."""
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 1)
		frappe.clear_cache()

	# --- minor units ------------------------------------------------------

	def test_create_session_charges_iso_minor_units_for_a_two_decimal_currency(self):
		self.get_settings().create_session(1234.56, "INR", reference="GPR-0001")

		self.assertEqual(FakeRazorpay.created_links[-1]["amount"], 123456)

	def test_create_session_charges_iso_minor_units_for_a_three_decimal_currency(self):
		"""KWD has three decimals. An int(amount * 100) would bill 1234 fils for 12.345 KWD."""
		self.get_settings().create_session(12.345, "KWD", reference="GPR-0002")

		charged = FakeRazorpay.created_links[-1]["amount"]
		self.assertEqual(charged, 12345)
		self.assertNotEqual(charged, int(12.345 * 100))

	def test_create_session_returns_the_link_id_and_short_url(self):
		session = self.get_settings().create_session(100, "INR", reference="GPR-0003")

		self.assertTrue(session["session_id"].startswith("plink_"))
		self.assertEqual(session["redirect_url"], f"https://rzp.io/i/{session['session_id']}")
		self.assertIn("reference_id=GPR-0003", session["success_url"])

	# --- status map -------------------------------------------------------

	def get_payment_status(self, status, payment_status=None, treat_authorised_as_paid=0):
		link_id = FakeRazorpay.register_link(status=status)
		if payment_status:
			FakeRazorpay.add_payment(link_id, payment_status)
		settings = self.get_settings()
		settings.treat_authorised_as_paid = treat_authorised_as_paid
		return settings.get_payment_status(link_id)

	def test_a_paid_link_is_paid(self):
		self.assertEqual(self.get_payment_status("paid"), "Paid")

	def test_an_open_link_is_pending(self):
		self.assertEqual(self.get_payment_status("created"), "Pending")

	def test_a_partially_paid_link_is_pending(self):
		"""Part of the money is not the money; releasing goods on this is a loss."""
		self.assertEqual(self.get_payment_status("partially_paid"), "Pending")

	def test_an_expired_link_is_expired(self):
		self.assertEqual(self.get_payment_status("expired"), "Expired")

	def test_a_cancelled_link_is_cancelled(self):
		self.assertEqual(self.get_payment_status("cancelled"), "Cancelled")

	def test_an_unrecognised_status_stays_pending(self):
		"""Never terminal and never Paid: an unmapped state can neither ship goods nor kill a live order."""
		self.assertEqual(self.get_payment_status("who_knows"), "Pending")
		self.assertEqual(self.get_payment_status(""), "Pending")
		self.assertEqual(self.get_payment_status("  PAID_ish "), "Pending")

	def test_the_status_is_matched_case_and_whitespace_insensitively(self):
		self.assertEqual(self.get_payment_status(" Paid "), "Paid")

	# --- treat_authorised_as_paid ----------------------------------------

	def test_an_authorised_payment_stays_pending_until_the_store_says_otherwise(self):
		"""On a manual-capture account, calling this Paid ships goods against uncaptured funds."""
		self.assertEqual(self.get_payment_status("created", "authorized", 0), "Pending")

	def test_an_authorised_payment_counts_as_paid_once_the_store_is_marked_auto_capture(self):
		"""With auto-capture, leaving an authorised payment Pending strands paid orders forever."""
		self.assertEqual(self.get_payment_status("created", "authorized", 1), "Paid")

	def test_the_switch_never_promotes_a_status_razorpay_did_not_authorise(self):
		self.assertEqual(self.get_payment_status("expired", None, 1), "Expired")
		self.assertEqual(self.get_payment_status("cancelled", None, 1), "Cancelled")
		self.assertEqual(self.get_payment_status("created", None, 1), "Pending")
		# A captured payment is already covered by the `paid` link status; the switch is not a shortcut
		# past a link Razorpay has not settled.
		self.assertEqual(self.get_payment_status("who_knows", "failed", 1), "Pending")

	def test_the_switch_never_promotes_a_terminal_link_carrying_an_authorised_payment(self):
		"""A cancelled or expired link is Razorpay's final word; an authorisation on it is not money."""
		self.assertEqual(self.get_payment_status("cancelled", "authorized", 1), "Cancelled")
		self.assertEqual(self.get_payment_status("expired", "authorized", 1), "Expired")

	# --- success url ------------------------------------------------------

	def test_build_success_url_appends_the_reference_id(self):
		success_url = self.get_settings().build_success_url("GPR-0007")

		self.assertEqual(success_url, f"{SUCCESS_URL}?reference_id=GPR-0007")

	def test_build_success_url_preserves_an_existing_query_string(self):
		settings = self.get_settings()
		settings.success_url = "https://shop.test/en/confirm?ref=abc"

		success_url = settings.build_success_url("GPR-0008")

		self.assertIn("ref=abc", success_url)
		self.assertIn("reference_id=GPR-0008", success_url)
		self.assertEqual(success_url.count("?"), 1)

	def test_build_success_url_throws_when_the_success_url_is_unset(self):
		settings = self.get_settings()
		settings.success_url = None

		with self.assertRaises(frappe.ValidationError):
			settings.build_success_url("GPR-0009")

	# --- refund -----------------------------------------------------------

	def test_refund_resolves_the_captured_payment_and_posts_minor_units(self):
		link_id = FakeRazorpay.register_link(status="paid")
		FakeRazorpay.add_payment(link_id, "failed")
		payment_id = FakeRazorpay.add_payment(link_id, "captured")

		refund = self.get_settings().refund_payment(link_id, 12.345, "KWD")

		self.assertEqual(FakeRazorpay.created_refunds[-1]["payment_id"], payment_id)
		self.assertEqual(FakeRazorpay.created_refunds[-1]["amount"], 12345)
		# The gateway's own echo round-tripped back to major units, so charge and refund agree.
		self.assertEqual(refund["amount"], 12.345)
		self.assertEqual(refund["status"], "processed")
		self.assertTrue(refund["refund_id"].startswith("rfnd_"))

	def test_a_pending_refund_is_accepted(self):
		link_id = FakeRazorpay.register_link(status="paid")
		FakeRazorpay.add_payment(link_id, "captured")
		FakeRazorpay.next_refund_status = "pending"

		self.assertEqual(self.get_settings().refund_payment(link_id, 100, "INR")["status"], "pending")

	def test_a_refund_throws_when_the_link_has_no_captured_payment(self):
		link_id = FakeRazorpay.register_link(status="created")
		FakeRazorpay.add_payment(link_id, "authorized")

		with self.assertRaises(frappe.ValidationError):
			self.get_settings().refund_payment(link_id, 100, "INR")

		self.assertEqual(FakeRazorpay.created_refunds, [])

	def test_a_refund_throws_when_the_gateway_reports_an_unaccepted_status(self):
		"""A `failed` refund must not be recorded as money returned to the shopper."""
		link_id = FakeRazorpay.register_link(status="paid")
		FakeRazorpay.add_payment(link_id, "captured")
		FakeRazorpay.next_refund_status = "failed"

		with self.assertRaises(frappe.ValidationError):
			self.get_settings().refund_payment(link_id, 100, "INR")

	def test_a_refund_on_an_unknown_link_throws_rather_than_refunding_nothing_quietly(self):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings().refund_payment("plink_does_not_exist", 100, "INR")

		self.assertEqual(FakeRazorpay.created_refunds, [])


class TestRazorpayWebhookVerification(RazorpayTestCase):
	"""`handle_webhook` in isolation: what the signature check accepts and what it hands the spine."""

	def handle(self, payload: bytes, signature: str | None = None, event_id: str | None = "evt_rzp_1"):
		headers = {"Content-Type": "application/json"}
		if signature is not None:
			headers["X-Razorpay-Signature"] = signature
		if event_id is not None:
			headers["X-Razorpay-Event-Id"] = event_id
		return self.get_settings().handle_webhook(payload, headers)

	def sign(self, payload: bytes, secret: str = RAZORPAY_WEBHOOK_SECRET) -> str:
		return sign_razorpay_payload(payload, secret)

	def test_a_correctly_signed_paid_event_is_mapped_to_the_link_id_and_status(self):
		payload = build_payment_link_paid_event("plink_signed_ok")

		result = self.handle(payload, self.sign(payload))

		# The payment link id, not the payment id: `order_ref` holds the link id.
		self.assertEqual(result["session_id"], "plink_signed_ok")
		self.assertEqual(result["status"], "Paid")
		self.assertEqual(result["event_id"], "evt_rzp_1")

	def test_a_forged_signature_is_rejected(self):
		payload = build_payment_link_paid_event("plink_forged")

		with self.assertRaises(frappe.ValidationError):
			self.handle(payload, self.sign(payload, "attacker_guess"))

	def test_a_signature_over_a_different_body_is_rejected(self):
		"""Signing a re-serialised body is the classic way to accept a tampered payload."""
		signature = self.sign(build_payment_link_paid_event("plink_other"))

		with self.assertRaises(frappe.ValidationError):
			self.handle(build_payment_link_paid_event("plink_swapped"), signature)

	def test_a_missing_signature_header_is_rejected(self):
		payload = build_payment_link_paid_event("plink_unsigned")

		with self.assertRaises(frappe.ValidationError):
			self.handle(payload, None)

	def test_the_key_secret_is_not_accepted_in_place_of_the_webhook_secret(self):
		"""Razorpay signs with the webhook secret; reaching for `key_secret` fails every delivery."""
		payload = build_payment_link_paid_event("plink_wrong_secret")

		with self.assertRaises(frappe.ValidationError):
			self.handle(payload, self.sign(payload, "rzp_test_secret"))

	def test_an_event_of_another_type_is_ignored(self):
		payload = build_payment_link_paid_event("plink_captured", event="payment.captured")

		self.assertEqual(self.handle(payload, self.sign(payload)), {})

	def test_the_event_id_is_read_from_the_razorpay_header(self):
		payload = build_payment_link_paid_event("plink_event_id")

		result = self.handle(payload, self.sign(payload), event_id="evt_from_header")

		self.assertEqual(result["event_id"], "evt_from_header")

	def test_an_unmapped_status_on_a_paid_event_throws_so_razorpay_retries(self):
		"""Swallowing it would 200 a paid order into Pending with nothing in the Error Log."""
		payload = build_payment_link_paid_event("plink_odd_status", status="who_knows")

		with self.assertRaises(frappe.ValidationError):
			self.handle(payload, self.sign(payload))

	def test_a_delivery_is_rejected_when_no_webhook_secret_is_configured(self):
		frappe.db.set_single_value("Razorpay Gateway Settings", "webhook_secret", "")
		frappe.clear_cache(doctype="Razorpay Gateway Settings")
		payload = build_payment_link_paid_event("plink_no_secret")

		with self.assertRaises(frappe.ValidationError):
			self.handle(payload, "anything")
