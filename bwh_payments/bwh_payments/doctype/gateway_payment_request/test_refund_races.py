# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils.data import flt

from bwh_payments.base_class import WebhookEvent
from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	GATEWAY,
	StripeGatewayTestCase,
	make_payment_request,
)
from bwh_payments.services import GatewayRefusedError, GatewayUnavailableError
from bwh_payments.services.stripe.stub import StubStripeClient
from bwh_payments.testing import commit_elsewhere, reserve_refund_elsewhere, snapshot_isolation


class TestRefundRaces(StripeGatewayTestCase):
	"""Another worker commits after this transaction took its snapshot.

	Without snapshot isolation a locking read sees what it committed. With it, the lock fails the
	transaction, and the operation run again in a fresh one sees it.
	"""

	snapshot_isolated = False

	def setUp(self):
		super().setUp()
		self.enterContext(snapshot_isolation(self.snapshot_isolated))

	def paid_request_with_a_snapshot(self):
		"""Committed, which frees the locks earlier tests hold, then read to take a new snapshot.

		Committed rows, and refunds reserved elsewhere, go with `remove_stripe_gateway`.
		"""
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		payment_request.reload()
		return payment_request

	def assert_restarted_with_snapshot_isolation(self, operation):
		"""The first run fails at its lock and is rolled back; the caller then runs it again."""
		if not self.snapshot_isolated:
			return
		with self.assertRaises(frappe.QueryDeadlockError):
			operation()
		frappe.db.rollback()

	def test_a_refund_reserved_by_another_worker_after_this_snapshot_counts_against_the_balance(self):
		payment_request = self.paid_request_with_a_snapshot()
		reserve_refund_elsewhere(payment_request.name, 70)

		self.assert_restarted_with_snapshot_isolation(lambda: payment_request.create_refund(70))
		with self.assertRaisesRegex(frappe.ValidationError, "exceeds the remaining refundable amount"):
			payment_request.create_refund(70)
		self.assertEqual(payment_request.get_pending_refund_amount(), 70)
		self.assertEqual(StubStripeClient.created_refunds, [])

	def test_a_refund_another_worker_settled_after_this_snapshot_counts_against_the_balance(self):
		payment_request = self.paid_request_with_a_snapshot()
		commit_elsewhere(
			"update `tabGateway Payment Request` set refund_amount = 70, status = 'Partially Refunded'"
			" where name = %s",
			(payment_request.name,),
		)

		self.assert_restarted_with_snapshot_isolation(lambda: payment_request.create_refund(70))
		with self.assertRaisesRegex(frappe.ValidationError, "exceeds the remaining refundable amount"):
			payment_request.create_refund(70)
		self.assertEqual(StubStripeClient.created_refunds, [])

	def test_a_refund_another_worker_settled_while_its_answer_was_on_the_way_is_kept(self):
		payment_request = self.paid_request_with_a_snapshot()
		refund_id = frappe.generate_hash()

		def settle_elsewhere_then_answer(*args, **kwargs):
			commit_elsewhere(
				"update `tabGateway Refund` set status = 'Processed', refund_id = %s, modified = now(6)"
				" where payment_request = %s",
				(refund_id, payment_request.name),
			)
			commit_elsewhere(
				"update `tabGateway Payment Request` set refund_amount = 40, status = 'Partially Refunded',"
				" modified = now(6) where name = %s",
				(payment_request.name,),
			)
			return {"refund_id": refund_id, "status": "processed"}

		settings_class = type(payment_request.get_gateway_settings())
		with patch.object(settings_class, "refund_payment", side_effect=settle_elsewhere_then_answer):
			refund = payment_request.create_refund(40)

		self.assertEqual((refund.status, refund.refund_id), ("Processed", refund_id))
		payment_request.lock_row()
		self.assertEqual(flt(payment_request.refund_amount), 40.0)

	def test_a_resend_refused_after_another_worker_settled_the_refund_keeps_that_outcome(self):
		payment_request = self.paid_request_with_a_snapshot()
		refund_id = frappe.generate_hash()

		def settle_elsewhere_then_refuse(*args, **kwargs):
			commit_elsewhere(
				"update `tabGateway Refund` set status = 'Processed', refund_id = %s, modified = now(6)"
				" where payment_request = %s",
				(refund_id, payment_request.name),
			)
			raise GatewayRefusedError("already refunded")

		self.refund_through_an_idempotent_gateway(
			payment_request, 40, GatewayUnavailableError("timed out"), settle_elsewhere_then_refuse
		)

		refund = frappe.db.get_value(
			"Gateway Refund",
			{"payment_request": payment_request.name},
			["status", "refund_id"],
			for_update=True,
		)
		self.assertEqual(refund, ("Processed", refund_id))

	def test_a_webhook_for_a_payment_another_worker_settled_after_this_snapshot_keeps_that_outcome(self):
		payment_request = make_payment_request(100, "SAR")
		log = webhook.log_webhook(
			GATEWAY,
			event=WebhookEvent(session_id=payment_request.order_ref, status="Expired", event_id="evt_late"),
			reference_docname=payment_request.name,
		)
		payment_request.reload()
		commit_elsewhere(
			"update `tabGateway Payment Request` set status = 'Paid' where name = %s", (payment_request.name,)
		)

		if self.snapshot_isolated:
			with self.assertRaises(frappe.RetryBackgroundJobError):
				webhook.process_webhook(log.name)
			frappe.db.rollback()
		webhook.process_webhook(log.name)

		payment_request.lock_row()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(frappe.db.get_value("Integration Request", log.name, "status"), "Completed")


class TestRefundRacesWithSnapshotIsolation(TestRefundRaces):
	snapshot_isolated = True
