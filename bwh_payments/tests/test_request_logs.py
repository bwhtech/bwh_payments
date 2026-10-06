# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.deferred_insert import save_to_db as save_deferred_inserts
from frappe.tests import IntegrationTestCase

# These gateways log every HTTP call, and those calls run inside a refund that holds a row lock.
HTTP_GATEWAY_SETTINGS = ("Razorpay Gateway Settings", "Telr Gateway Settings", "Tabby Gateway Settings")


class TestGatewayRequestLogs(IntegrationTestCase):
	def test_logging_a_gateway_call_leaves_the_callers_transaction_open(self):
		"""A commit here released the refund's row lock in the middle of its gateway calls."""
		for settings_doctype in HTTP_GATEWAY_SETTINGS:
			with self.subTest(settings_doctype):
				marker = frappe.get_doc(
					{"doctype": "ToDo", "description": "uncommitted caller work"}
				).insert()

				frappe.get_single(settings_doctype).log_request(
					f"https://gateway.test/{marker.name}", output={"id": "x"}
				)
				frappe.db.rollback()

				self.assertFalse(frappe.db.exists("ToDo", marker.name))

	def test_a_failed_gateway_call_is_recorded_after_the_rollback(self):
		"""A gateway error is followed by a rollback, and the log is how an operator learns what it said."""
		for settings_doctype in HTTP_GATEWAY_SETTINGS:
			with self.subTest(settings_doctype):
				endpoint = f"https://gateway.test/{frappe.generate_hash(length=12)}"

				frappe.get_single(settings_doctype).log_request(endpoint, error="invalid request sent")
				frappe.db.rollback()
				save_deferred_inserts("Integration Request")

				statuses = frappe.get_all(
					"Integration Request", filters={"data": ["like", f"%{endpoint}%"]}, pluck="status"
				)
				self.assertEqual(statuses, ["Failed"])
				# Committed by the flush above, so remove it by hand.
				frappe.db.delete("Integration Request", {"data": ["like", f"%{endpoint}%"]})
				frappe.db.commit()
