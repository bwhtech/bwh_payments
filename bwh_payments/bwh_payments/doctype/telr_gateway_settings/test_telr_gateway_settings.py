# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.services.telr.base import TelrOrder
from bwh_payments.services.telr.stub import StubTelrClient

RETURN_URL = (
	"https://shop.test/api/method/bwh_payments.bwh_payments.checkout.return_from_gateway?request=GPR-1"
)


class TestTelrGatewaySettings(IntegrationTestCase):
	def setUp(self):
		StubTelrClient.reset()

	def get_payment_status(self, status_text, treat_authorised_as_paid):
		settings = frappe.get_single("Telr Gateway Settings")
		settings.treat_authorised_as_paid = treat_authorised_as_paid
		return settings.get_payment_status(StubTelrClient.add_order(status_text))

	def test_a_settled_order_is_traced_to_its_transaction(self):
		order_ref = StubTelrClient.add_order("Paid")
		transaction_ref = StubTelrClient.pay(order_ref)

		self.assertEqual(
			frappe.get_single("Telr Gateway Settings").get_transaction_ref(order_ref), transaction_ref
		)

	def test_an_order_without_a_transaction_has_no_reference(self):
		order_ref = StubTelrClient.add_order("Pending")

		self.assertIsNone(frappe.get_single("Telr Gateway Settings").get_transaction_ref(order_ref))

	def test_telr_returns_every_outcome_to_the_given_return_url(self):
		settings = frappe.get_single("Telr Gateway Settings")
		settings.update(
			{
				"store_id": "1",
				"currency": "AED",
				"authorised_url": "",
				"declined_url": "",
				"cancelled_url": "",
			}
		)

		settings.create_session(100, "AED", reference="GPR-1", return_url=RETURN_URL)

		returns = StubTelrClient.created_orders[-1]["return"]
		self.assertEqual(set(returns.values()), {RETURN_URL})

	def test_an_authorised_order_stays_pending_until_the_store_says_otherwise(self):
		self.assertEqual(self.get_payment_status("Authorised", 0), "Pending")

	def test_an_authorised_order_counts_as_paid_once_the_store_is_marked_auth_and_capture(self):
		self.assertEqual(self.get_payment_status("Authorised", 1), "Paid")

	def test_the_american_spelling_is_mapped_the_same_way(self):
		self.assertEqual(self.get_payment_status("authorized", 1), "Paid")
		self.assertEqual(self.get_payment_status("authorized", 0), "Pending")

	def test_the_switch_never_promotes_a_status_telr_did_not_authorise(self):
		self.assertEqual(self.get_payment_status("Declined", 1), "Not Paid")
		self.assertEqual(self.get_payment_status("Cancelled", 1), "Cancelled")
		self.assertEqual(self.get_payment_status("Who Knows", 1), "Pending")

	def test_create_session_bills_major_units_and_brings_the_shopper_back_with_our_reference(self):
		settings = frappe.get_single("Telr Gateway Settings")
		settings.authorised_url = "https://shop.test/en/confirm?ref=abc"

		session = settings.create_session(12.345, "KWD", reference="GPR-0001")

		self.assertEqual(StubTelrClient.created_orders[-1]["order"]["amount"], "12.345")
		self.assertEqual(StubTelrClient.created_orders[-1]["order"]["cartid"], "GPR-0001")
		self.assertIn(session.session_id, StubTelrClient.orders)
		self.assertIn("ref=abc", session.success_url)
		self.assertIn("reference_id=GPR-0001", session.success_url)

	def get_settings_with_remote_key(self):
		settings = frappe.get_single("Telr Gateway Settings")
		settings.remote_auth_key = "telr_remote_key"
		return settings

	def test_a_refund_goes_against_the_settled_transaction_at_the_currency_precision(self):
		order_ref = StubTelrClient.add_order()
		transaction_ref = StubTelrClient.pay(order_ref)

		refund = self.get_settings_with_remote_key().refund_payment(order_ref, 12.3456, "KWD")

		self.assertEqual(
			StubTelrClient.refunds, [{"ref": transaction_ref, "amount": "12.346", "currency": "KWD"}]
		)
		self.assertEqual(refund.status, "succeeded")
		self.assertEqual(refund.amount, 12.3456)

	def test_a_refund_telr_declines_throws(self):
		order_ref = StubTelrClient.add_order()
		StubTelrClient.pay(order_ref)
		StubTelrClient.next_refund_status = "D"

		with self.assertRaises(frappe.ValidationError):
			self.get_settings_with_remote_key().refund_payment(order_ref, 10, "AED")

	def test_an_order_with_no_settled_transaction_is_never_refunded(self):
		order_ref = StubTelrClient.add_order("Authorised")

		with self.assertRaises(frappe.ValidationError):
			self.get_settings_with_remote_key().refund_payment(order_ref, 10, "AED")

		self.assertEqual(StubTelrClient.refunds, [])

	def test_a_numeric_order_ref_from_telr_is_read_as_text(self):
		order = TelrOrder.model_validate({"ref": 1234567, "transaction": {"ref": 4002201}})

		self.assertEqual(order.ref, "1234567")
		self.assertEqual(order.transaction.ref, "4002201")
