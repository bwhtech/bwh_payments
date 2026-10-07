# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import json

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.services.tabby.stub import StubTabbyClient

WEBHOOK_TOKEN = "tabby_webhook_token"


class TestTabbyGatewaySettings(IntegrationTestCase):
	def setUp(self):
		StubTabbyClient.reset()
		self.settings = frappe.get_single("Tabby Gateway Settings")
		self.settings.update(
			{
				"merchant_code": "zz_merchant",
				"currency": "SAR",
				"webhook_secret": WEBHOOK_TOKEN,
				"webhook_ips": "",
				"success_url": "https://shop.test/en/confirm",
				"cancelled_url": "https://shop.test/en/cart",
				"failure_url": "https://shop.test/en/cart/checkout",
			}
		)

	# --- session ----------------------------------------------------------

	def test_create_session_keys_the_request_on_the_payment_id_not_the_checkout_id(self):
		session = self.settings.create_session(12.345, "KWD", reference="GPR-0001")

		self.assertIn(session.session_id, StubTabbyClient.payments)
		self.assertEqual(session.redirect_url, f"https://checkout.tabby.test/{session.session_id}")
		self.assertIn("reference_id=GPR-0001", session.success_url)
		# KWD carries three decimals; a plain str(float) would under-specify it.
		self.assertEqual(StubTabbyClient.created_checkouts[-1]["payment"]["amount"], "12.345")

	def test_a_currency_tabby_does_not_underwrite_is_refused_before_any_call(self):
		with self.assertRaises(frappe.ValidationError):
			self.settings.create_session(100, "INR", reference="GPR-0002")

		self.assertEqual(StubTabbyClient.created_checkouts, [])

	def test_a_declined_shopper_is_told_why_in_words_they_can_act_on(self):
		StubTabbyClient.reject_next_checkout("order_amount_too_high")

		with self.assertRaises(frappe.ValidationError) as raised:
			self.settings.create_session(100, "SAR", reference="GPR-0003")

		self.assertIn("above Tabby's limit", str(raised.exception))

	# --- status and capture -----------------------------------------------

	def test_reading_an_authorised_payment_captures_it_for_the_authorised_amount(self):
		payment_id = StubTabbyClient.add_payment(amount="250.5", currency="SAR")
		StubTabbyClient.authorise(payment_id)

		self.assertEqual(self.settings.get_payment_status(payment_id), "Paid")
		self.assertEqual(StubTabbyClient.captures, [{"payment_id": payment_id, "amount": "250.50"}])

	def test_a_payment_still_being_decided_is_pending_and_not_captured(self):
		payment_id = StubTabbyClient.add_payment(status="created")

		self.assertEqual(self.settings.get_payment_status(payment_id), "Pending")
		self.assertEqual(StubTabbyClient.captures, [])

	def test_a_rejected_payment_is_not_paid(self):
		payment_id = StubTabbyClient.add_payment(status="rejected")

		self.assertEqual(self.settings.get_payment_status(payment_id), "Not Paid")

	# --- refund -----------------------------------------------------------

	def test_a_captured_payment_refunds_and_reports_tabbys_refund_id(self):
		payment_id = StubTabbyClient.add_payment(status="closed")

		refund = self.settings.refund_payment(payment_id, 12.345, "KWD")

		self.assertEqual(StubTabbyClient.refunds, [{"payment_id": payment_id, "amount": "12.345"}])
		self.assertEqual(refund.refund_id, StubTabbyClient.payments[payment_id].refunds[-1].id)
		self.assertEqual(refund.status, "succeeded")

	def test_an_uncaptured_payment_is_never_refunded(self):
		payment_id = StubTabbyClient.add_payment()
		StubTabbyClient.authorise(payment_id)

		with self.assertRaises(frappe.ValidationError):
			self.settings.refund_payment(payment_id, 10, "SAR")

		self.assertEqual(StubTabbyClient.refunds, [])

	# --- webhook ----------------------------------------------------------

	def deliver(self, token: str | None, request_ip: str = "127.0.0.1"):
		self.addCleanup(setattr, frappe.local, "request_ip", getattr(frappe.local, "request_ip", None))
		frappe.local.request_ip = request_ip
		payload = json.dumps(
			{"id": "pay_tabby_hook", "status": "closed", "updated_at": "2026-10-07"}
		).encode()
		headers = {"X-Webhook-Signature": token} if token is not None else {}
		return self.settings.handle_webhook(payload, headers)

	def test_a_delivery_carrying_the_registered_token_is_mapped(self):
		event = self.deliver(WEBHOOK_TOKEN)

		self.assertEqual(event.session_id, "pay_tabby_hook")
		self.assertEqual(event.status, "Paid")
		self.assertTrue(event.event_id)

	def test_a_wrong_or_missing_token_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.deliver("guess")
		with self.assertRaises(frappe.ValidationError):
			self.deliver(None)

	def test_a_delivery_from_outside_the_allowlist_is_rejected_even_with_the_token(self):
		self.settings.webhook_ips = "203.0.113.10"

		with self.assertRaises(frappe.ValidationError):
			self.deliver(WEBHOOK_TOKEN, request_ip="198.51.100.7")
