# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.geo.doctype.currency.currency import Currency
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments import testing
from bwh_payments.api import create_payment, refund, sync_refunds


class TestConsumerTestHelpers(IntegrationTestCase):
	def setUp(self):
		testing.reset()
		self.gateway = testing.configure_test_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		frappe.db.rollback()
		testing.remove_test_gateway()
		frappe.db.commit()

	def make_paid_payment(self):
		payment_request = create_payment("Currency", "INR", 100, "INR", self.gateway)
		testing.pay(payment_request.name)
		payment_request.reload()
		return payment_request

	def test_pay_settles_the_payment_and_tells_the_document(self):
		payment_request = create_payment("Currency", "INR", 100, "INR", self.gateway)

		with patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized:
			testing.pay(payment_request.name)

		on_payment_authorized.assert_called_once_with("Completed")
		self.assertEqual(
			frappe.db.get_value("Gateway Payment Request", payment_request.name, "status"), "Paid"
		)

	def test_a_pending_refund_settles_when_its_webhook_arrives(self):
		payment_request = self.make_paid_payment()
		testing.set_next_refund_status("Pending")
		gateway_refund = refund(payment_request.name, 40)

		testing.send_refund_webhook(gateway_refund.name, "Processed")

		gateway_refund.reload()
		self.assertEqual(gateway_refund.status, "Processed")

	def test_a_refund_raised_outside_the_app_is_recorded(self):
		payment_request = self.make_paid_payment()

		testing.send_external_refund_webhook(payment_request.name, 25, "Processed")

		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 25)

	def test_a_refund_the_gateway_refuses_raises(self):
		payment_request = self.make_paid_payment()
		testing.set_next_refund_status("Failed")

		with self.assertRaises(frappe.ValidationError):
			refund(payment_request.name, 40)

	def test_a_pending_refund_settles_when_synced_after_the_gateway_reports_it(self):
		payment_request = self.make_paid_payment()
		testing.set_next_refund_status("Pending")
		gateway_refund = refund(payment_request.name, 40)

		testing.set_refund_status(gateway_refund.name, "Processed")
		sync_refunds(payment_request.name)

		gateway_refund.reload()
		self.assertEqual(gateway_refund.status, "Processed")

	def test_without_commits_keeps_a_refund_inside_the_test_transaction(self):
		payment_request = self.make_paid_payment()

		with testing.without_commits():
			gateway_refund = refund(payment_request.name, 40)
		frappe.db.rollback()

		self.assertFalse(frappe.db.exists("Gateway Refund", gateway_refund.name))
