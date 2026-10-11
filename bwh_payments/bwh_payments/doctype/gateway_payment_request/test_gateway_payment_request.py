# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.geo.doctype.currency.currency import Currency
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments.api import create_payment
from bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request import (
	GatewayPaymentRequest,
)
from bwh_payments.currency import to_minor_units
from bwh_payments.services.stripe.base import StripeRefund
from bwh_payments.services.stripe.stub import StubStripeClient

GATEWAY = "Stripe Test Gateway"
WEBHOOK_SECRET = "whsec_test_secret"

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


def remove_stripe_gateway():
	"""`create_request_log` commits, so refund tests escape the rollback and need this cleanup."""
	frappe.db.delete(
		"Gateway Refund",
		{
			"payment_request": [
				"in",
				frappe.get_all("Gateway Payment Request", {"gateway": GATEWAY}, pluck="name"),
			]
		},
	)
	for request_name in frappe.get_all("Gateway Payment Request", filters={"gateway": GATEWAY}, pluck="name"):
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	frappe.db.delete("Integration Request", {"integration_request_service": f"{GATEWAY} Webhook"})

	frappe.delete_doc(
		"Payment Gateway Profile", GATEWAY, ignore_missing=True, ignore_permissions=True, force=True
	)
	# Not save(): the keys are mandatory.
	frappe.db.set_single_value(
		"Stripe Gateway Settings",
		{"enabled": 0, "public_key": "", "private_key": "", "webhook_secret": ""},
	)
	frappe.clear_cache(doctype="Stripe Gateway Settings")


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
		StubStripeClient.reset()
		configure_stripe_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Commit only the cleanup; frappe's class-level rollback would undo it otherwise.
		frappe.db.rollback()
		remove_stripe_gateway()
		frappe.db.commit()

	def use_currency_number_format(self):
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 1)
		frappe.clear_cache()

	def mark_paid(self, payment_request):
		StubStripeClient.pay(payment_request.order_ref)
		payment_request.db_set("status", "Paid", update_modified=False)
		payment_request.reload()

	def test_create_session_charges_iso_minor_units_for_a_three_decimal_currency(self):
		self.use_currency_number_format()
		payment_request = make_payment_request(12.345, "KWD")

		charged = StubStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.assertEqual(charged, 12345)
		self.assertNotEqual(charged, int(12.345 * 100))
		self.assertTrue(payment_request.order_ref)

	def test_a_three_decimal_amount_is_refused_when_the_site_stores_only_two(self):
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 0)
		frappe.clear_cache()

		with self.assertRaises(frappe.ValidationError):
			make_payment_request(12.345, "KWD")

		self.assertEqual(StubStripeClient.created_sessions, [])

	def test_create_session_charges_iso_minor_units_for_a_zero_decimal_currency(self):
		make_payment_request(1000, "JPY")

		charged = StubStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.assertEqual(charged, 1000)
		self.assertNotEqual(charged, 1000 * 100)

	def test_success_url_keeps_the_stripe_placeholder_and_existing_query(self):
		settings = frappe.get_single("Stripe Gateway Settings")
		settings.db_set("success_url", "https://shop.test/en/confirm?ref=abc", update_modified=False)
		settings.reload()

		success_url = settings.build_success_url()

		self.assertIn("ref=abc", success_url)
		self.assertIn("session_id={CHECKOUT_SESSION_ID}", success_url)
		self.assertEqual(success_url.count("?"), 1)

	def test_charge_and_refund_agree_on_the_minor_unit_conversion(self):
		self.use_currency_number_format()
		payment_request = make_payment_request(12.345, "KWD")
		charged = StubStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.mark_paid(payment_request)

		payment_request.refund()

		self.assertEqual(StubStripeClient.created_refunds[-1]["amount"], charged)
		self.assertEqual(to_minor_units(payment_request.refund_amount, "KWD"), charged)
		self.assertEqual(payment_request.status, "Refunded")

	def test_refund_guard_reads_the_locked_ledger_not_the_in_memory_copy(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		# Stand in for a refund committed by another request after this document was loaded.
		frappe.db.set_value(
			"Gateway Payment Request", payment_request.name, "refund_amount", 100, update_modified=False
		)
		self.assertEqual(flt(payment_request.refund_amount), 0.0)

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(100)

		self.assertEqual(StubStripeClient.created_refunds, [])

	def test_refund_id_is_appended_once_per_successful_partial(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		payment_request.refund(30)
		payment_request.refund(30)
		payment_request.refund(30)

		self.assertEqual(len(payment_request.refund_id.split(",")), 3)
		self.assertEqual(len(set(payment_request.refund_id.split(","))), 3)
		self.assertEqual(flt(payment_request.refund_amount), 90.0)
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_over_refund_is_rejected_and_never_reaches_the_gateway(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		payment_request.refund(60)
		StubStripeClient.created_refunds.clear()

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(41)

		self.assertEqual(StubStripeClient.created_refunds, [])
		self.assertEqual(flt(payment_request.refund_amount), 60.0)

	def test_an_omitted_amount_refunds_the_whole_remaining_balance(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		payment_request.refund(40)

		payment_request.refund()

		self.assertEqual(flt(payment_request.refund_amount), 100.0)
		self.assertEqual(payment_request.status, "Refunded")

	def test_a_sub_minor_unit_amount_is_rejected_rather_than_refunding_everything(self):
		payment_request = make_payment_request(500, "SAR")
		self.mark_paid(payment_request)

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(0.004)

		self.assertEqual(StubStripeClient.created_refunds, [])
		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 0.0)
		self.assertEqual(payment_request.status, "Paid")

	def test_an_explicit_zero_is_rejected_rather_than_refunding_everything(self):
		payment_request = make_payment_request(500, "SAR")
		self.mark_paid(payment_request)

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(0)

		self.assertEqual(StubStripeClient.created_refunds, [])
		self.assertEqual(flt(payment_request.refund_amount), 0.0)

	def test_a_residue_under_a_whole_unit_stays_refundable(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		payment_request.refund(99.01)
		self.assertEqual(payment_request.status, "Partially Refunded")

		payment_request.refund()

		self.assertEqual(flt(payment_request.refund_amount), 100.0)
		self.assertEqual(payment_request.status, "Refunded")
		self.assertEqual(StubStripeClient.created_refunds[-1]["amount"], to_minor_units(0.99, "SAR"))

	def test_a_whole_unit_left_is_still_only_partially_refunded(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		payment_request.refund(99)

		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_refund_rejected_while_the_payment_is_still_pending(self):
		payment_request = make_payment_request(100, "SAR")

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(10)

		self.assertEqual(StubStripeClient.created_refunds, [])

	def test_a_failed_gateway_refund_leaves_the_ledger_untouched(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		StubStripeClient.next_refund_status = "failed"

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(50)

		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 0.0)
		self.assertIsNone(payment_request.refund_id)

	def test_a_gateway_that_returns_no_refund_details_still_books_the_refund(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		with patch.object(type(payment_request.get_gateway_settings()), "refund_payment", return_value=None):
			payment_request.refund(40)

		self.assertEqual(flt(payment_request.refund_amount), 40.0)
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_a_numeric_refund_id_from_another_apps_gateway_still_books_the_refund(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		with patch.object(
			type(payment_request.get_gateway_settings()), "refund_payment", return_value={"refund_id": 12345}
		):
			payment_request.refund(40)

		self.assertEqual(payment_request.refund_id, "12345")
		self.assertEqual(flt(payment_request.refund_amount), 40.0)

	def test_a_refund_echo_without_id_or_amount_still_books_the_refund(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		with patch.object(StubStripeClient, "create_refund", return_value=StripeRefund(status="succeeded")):
			payment_request.refund(40)

		self.assertEqual(flt(payment_request.refund_amount), 40.0)
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_an_abandoned_session_is_released_and_expired_at_the_gateway(self):
		payment_request = make_payment_request(100, "SAR")

		self.assertTrue(payment_request.release_if_unpaid())
		self.assertEqual(payment_request.status, "Cancelled")
		self.assertEqual(StubStripeClient.sessions[payment_request.order_ref].status, "expired")

	def test_a_paid_session_keeps_its_payment_and_is_never_expired(self):
		payment_request = make_payment_request(100, "SAR")
		StubStripeClient.pay(payment_request.order_ref)

		self.assertFalse(payment_request.release_if_unpaid())
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(StubStripeClient.sessions[payment_request.order_ref].status, "complete")

	def test_a_session_the_gateway_will_not_cancel_stays_pending(self):
		payment_request = make_payment_request(100, "SAR")
		with patch.object(type(payment_request.get_gateway_settings()), "cancel_session", return_value=False):
			self.assertFalse(payment_request.release_if_unpaid())

		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_gateway_that_cannot_cancel_at_all_never_releases_the_cart(self):
		from bwh_payments.base_class import PaymentGatewayBase

		payment_request = make_payment_request(100, "SAR")
		self.assertFalse(PaymentGatewayBase.cancel_session(object(), payment_request.order_ref))

	def test_sync_status_does_not_reopen_a_payment_a_webhook_already_settled(self):
		payment_request = make_payment_request(100, "SAR")

		# Stand in for a webhook that committed while the gateway round-trip was in flight.
		frappe.db.set_value(
			"Gateway Payment Request", payment_request.name, "status", "Cancelled", update_modified=False
		)

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Cancelled")
		self.assertEqual(
			frappe.db.get_value("Gateway Payment Request", payment_request.name, "status"), "Cancelled"
		)

	def test_sync_status_writes_a_refund_status_over_a_gateway_paid(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		payment_request.refund(40)

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_a_replayed_webhook_event_is_ignored(self):
		payment_request = make_payment_request(100, "SAR")

		self.assertTrue(payment_request.apply_webhook_status("Paid", "evt_1"))
		self.assertFalse(payment_request.apply_webhook_status("Paid", "evt_1"))
		self.assertEqual(payment_request.status, "Paid")

	def test_a_webhook_cannot_reopen_a_settled_payment(self):
		payment_request = make_payment_request(100, "SAR")
		payment_request.apply_webhook_status("Paid", "evt_1")

		self.assertFalse(payment_request.apply_webhook_status("Cancelled", "evt_2"))
		self.assertEqual(payment_request.status, "Paid")

	def test_a_webhook_cannot_write_a_refund_status(self):
		payment_request = make_payment_request(100, "SAR")

		self.assertFalse(payment_request.apply_webhook_status("Refunded", "evt_1"))
		self.assertEqual(payment_request.status, "Pending")

	def test_the_referenced_document_hears_about_a_payment_once(self):
		payment_request = make_payment_request(100, "SAR")

		with patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized:
			payment_request.apply_webhook_status("Paid", "evt_1")
			payment_request.sync_status()

		on_payment_authorized.assert_called_once_with("Completed")

	def test_a_payment_that_did_not_settle_is_not_announced(self):
		payment_request = make_payment_request(100, "SAR")

		with patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized:
			payment_request.apply_webhook_status("Cancelled", "evt_1")

		on_payment_authorized.assert_not_called()

	def test_create_payment_opens_a_checkout_for_the_document(self):
		payment_request = create_payment(
			"Currency", "SAR", 100, "SAR", GATEWAY, customer_email="shopper@example.com"
		)

		self.assertEqual(payment_request.status, "Pending")
		self.assertTrue(payment_request.order_url)
		self.assertEqual(payment_request.customer_email, "shopper@example.com")

	def test_create_payment_refuses_fields_beyond_company_and_customer_details(self):
		with self.assertRaises(frappe.ValidationError):
			create_payment("Currency", "SAR", 100, "SAR", GATEWAY, status="Paid")

		self.assertEqual(StubStripeClient.created_sessions, [])

	def test_order_ref_is_unique(self):
		first = make_payment_request(100, "SAR")

		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(
				{
					"doctype": "Gateway Payment Request",
					"gateway": GATEWAY,
					"amount": 100,
					"currency_code": "SAR",
					"ref_doctype": "Currency",
					"ref_docname": "SAR",
					"order_ref": first.order_ref,
				}
			).insert(ignore_permissions=True)

	def test_gateway_payment_request_is_not_submittable(self):
		self.assertFalse(frappe.get_meta("Gateway Payment Request").is_submittable)

	def test_a_new_checkout_cannot_start_out_already_paid(self):
		with (
			patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized,
			self.assertRaises(frappe.ValidationError),
		):
			frappe.get_doc(
				{
					"doctype": "Gateway Payment Request",
					"gateway": GATEWAY,
					"amount": 100,
					"currency_code": "SAR",
					"ref_doctype": "Currency",
					"ref_docname": "SAR",
					"status": "Paid",
				}
			).insert(ignore_permissions=True)

		on_payment_authorized.assert_not_called()
		self.assertEqual(StubStripeClient.created_sessions, [])

	def test_importing_a_settled_payment_does_not_settle_the_document_again(self):
		with patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized:
			imported = frappe.get_doc(
				{
					"doctype": "Gateway Payment Request",
					"gateway": GATEWAY,
					"amount": 100,
					"currency_code": "SAR",
					"ref_doctype": "Currency",
					"ref_docname": "SAR",
					"status": "Paid",
					"order_ref": "cs_imported_1",
				}
			).insert(ignore_permissions=True)

		on_payment_authorized.assert_not_called()
		self.assertEqual(imported.status, "Paid")
		self.assertEqual(StubStripeClient.created_sessions, [])

	def test_what_the_shopper_was_charged_cannot_change_after_checkout_opens(self):
		payment_request = make_payment_request(100, "SAR")
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": "Another Gateway",
				"gateway_settings": "Stripe Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

		for field, value in (
			("amount", 1),
			("currency_code", "USD"),
			("gateway", "Another Gateway"),
			("order_ref", "cs_swapped"),
		):
			with self.subTest(field=field):
				changed = frappe.get_doc("Gateway Payment Request", payment_request.name)
				changed.set(field, value)
				with self.assertRaises(frappe.CannotChangeConstantError):
					changed.save(ignore_permissions=True)

	def test_a_paid_request_remembers_the_transaction_the_gateway_settled(self):
		payment_request = make_payment_request(100, "SAR")
		StubStripeClient.pay(payment_request.order_ref)

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.gateway_transaction_ref, f"pi_{payment_request.order_ref}")

	def test_a_transaction_lookup_that_fails_does_not_stop_the_payment_settling(self):
		payment_request = make_payment_request(100, "SAR")
		settings_class = type(payment_request.get_gateway_settings())

		with patch.object(settings_class, "get_transaction_ref", side_effect=frappe.ValidationError):
			payment_request.apply_webhook_status("Paid", "evt_ref_down")

		self.assertEqual(payment_request.status, "Paid")
		self.assertIsNone(payment_request.gateway_transaction_ref)

	def test_money_moving_methods_cannot_be_called_over_rest(self):
		frappe.is_whitelisted(GatewayPaymentRequest.sync_status)

		for method in (GatewayPaymentRequest.refund, GatewayPaymentRequest.release_if_unpaid):
			with self.subTest(method=method.__name__), self.assertRaises(frappe.PermissionError):
				frappe.is_whitelisted(method)
