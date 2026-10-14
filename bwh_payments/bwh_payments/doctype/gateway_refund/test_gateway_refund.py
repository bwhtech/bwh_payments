# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.geo.doctype.currency.currency import Currency
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments.api import record_external_refund
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	configure_stripe_gateway,
	make_payment_request,
	remove_stripe_gateway,
)
from bwh_payments.bwh_payments.doctype.gateway_refund.gateway_refund import normalise_refund_status
from bwh_payments.services.stripe.stub import StubStripeClient

IGNORE_TEST_RECORD_DEPENDENCIES = ["Address", "Company", "Customer", "Payment Request"]


class TestGatewayRefund(IntegrationTestCase):
	def setUp(self):
		StubStripeClient.reset()
		configure_stripe_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Refunds commit their reservation, so they outlive the class rollback.
		frappe.db.rollback()
		remove_stripe_gateway()
		frappe.db.commit()

	def make_paid_request(self, amount: float = 100):
		payment_request = make_payment_request(amount, "SAR")
		StubStripeClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		return payment_request

	def test_a_refund_is_recorded_with_the_gateway_id_and_the_callers_reference(self):
		payment_request = self.make_paid_request()

		refund = payment_request.create_refund(30, reference="BOOKING-REFUND-1")

		self.assertEqual(refund.status, "Processed")
		self.assertTrue(refund.refund_id.startswith("re_"))
		self.assertEqual(refund.reference, "BOOKING-REFUND-1")
		self.assertEqual(flt(refund.amount), 30)
		self.assertEqual(refund.currency, "SAR")

	def test_an_imported_refund_without_an_amount_is_refused(self):
		payment_request = self.make_paid_request()

		with self.assertRaises(frappe.ValidationError):
			record_external_refund(payment_request.name, "re_imported_zero", "processed", 0)

	def test_a_pending_refund_is_not_counted_as_refunded_yet(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"

		refund = payment_request.create_refund(30)

		payment_request.reload()
		self.assertEqual(refund.status, "Pending")
		self.assertEqual(flt(payment_request.refund_amount), 0)
		self.assertEqual(payment_request.status, "Paid")

	def test_a_pending_refund_holds_its_amount_against_further_refunds(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"
		payment_request.create_refund(60)
		StubStripeClient.created_refunds.clear()

		with self.assertRaises(frappe.ValidationError):
			payment_request.create_refund(41)

		self.assertEqual(StubStripeClient.created_refunds, [])

	def test_the_reservation_is_committed_before_the_gateway_is_asked(self):
		payment_request = self.make_paid_request()
		settings_class = type(payment_request.get_gateway_settings())
		original_refund_payment = settings_class.refund_payment
		seen_by_another_connection = []

		def refund_payment(settings, session_id, amount, currency=None):
			# A fresh read after rollback sees only what was committed.
			frappe.db.rollback()
			seen_by_another_connection.extend(
				frappe.get_all(
					"Gateway Refund",
					filters={"payment_request": payment_request.name},
					fields=["status", "amount"],
				)
			)
			return original_refund_payment(settings, session_id, amount, currency)

		with patch.object(settings_class, "refund_payment", refund_payment):
			payment_request.create_refund(25)

		self.assertEqual(
			[(row.status, flt(row.amount)) for row in seen_by_another_connection], [("Pending", 25)]
		)

	def test_a_refund_the_gateway_refuses_is_failed_and_frees_its_amount(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "failed"

		with self.assertRaises(frappe.ValidationError):
			payment_request.create_refund(100)

		[refund] = frappe.get_all(
			"Gateway Refund", filters={"payment_request": payment_request.name}, fields=["status", "error"]
		)
		self.assertEqual(refund.status, "Failed")
		self.assertTrue(refund.error)

		StubStripeClient.next_refund_status = "succeeded"
		self.assertEqual(payment_request.create_refund(100).status, "Processed")

	def test_the_referenced_document_hears_about_every_refund_status(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"

		statuses = []
		with patch.object(
			Currency,
			"on_refund_update",
			create=True,
			side_effect=lambda refund: statuses.append(refund.status),
		):
			refund = payment_request.create_refund(30)
			refund.apply_gateway_status("Processed")

		self.assertEqual(statuses, ["Pending", "Processed"])

	def test_a_settled_refund_is_counted_once_however_often_it_is_reported(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"
		refund = payment_request.create_refund(30)

		refund.apply_gateway_status("Processed")
		refund.apply_gateway_status("Processed")

		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 30)
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_a_settled_refund_cannot_be_reopened(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(30)

		self.assertFalse(refund.apply_gateway_status("Failed"))
		self.assertEqual(refund.status, "Processed")

	def test_a_payment_entry_is_refunded_once_even_while_its_refund_is_pending(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"
		module = "bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request"

		with patch(f"{module}.get_original_payment_entry", side_effect=lambda name: name):
			payment_request.refund(30, payment_entry="ACC-PAY-0001")
			payment_request.refund(30, payment_entry="ACC-PAY-0001")

		self.assertEqual(len(StubStripeClient.created_refunds), 1)

	def test_syncing_settles_a_pending_refund_from_what_the_gateway_reports(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"
		refund = payment_request.create_refund(30)
		StubStripeClient.set_refund_status(refund.refund_id, "succeeded")

		payment_request.sync_refunds()

		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(flt(payment_request.refund_amount), 30)

	def test_syncing_leaves_a_refund_pending_when_the_gateway_cannot_report_it(self):
		payment_request = self.make_paid_request()
		StubStripeClient.next_refund_status = "pending"
		refund = payment_request.create_refund(30)
		settings_class = type(payment_request.get_gateway_settings())

		with patch.object(settings_class, "get_refund_status", return_value=None):
			payment_request.sync_refunds()

		refund.reload()
		self.assertEqual(refund.status, "Pending")

	def test_only_a_user_who_can_edit_the_payment_can_sync_its_refunds(self):
		payment_request = self.make_paid_request()

		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			payment_request.sync_refunds()

	def test_gateway_refund_statuses_map_to_three_outcomes(self):
		for gateway_status, expected in (
			("succeeded", "Processed"),
			("processed", "Processed"),
			("Processed", "Processed"),
			(None, "Processed"),
			("pending", "Pending"),
			("requires_action", "Pending"),
			("failed", "Failed"),
			("canceled", "Failed"),
			("cancelled", "Failed"),
		):
			with self.subTest(gateway_status=gateway_status):
				self.assertEqual(normalise_refund_status(gateway_status), expected)
