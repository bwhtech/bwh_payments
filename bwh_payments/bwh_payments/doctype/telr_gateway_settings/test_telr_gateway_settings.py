# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

AUTHORISED_ORDER = {"status": {"text": "Authorised"}}


class TestTelrGatewaySettings(IntegrationTestCase):
	def get_payment_status(self, order, treat_authorised_as_paid):
		settings = frappe.get_single("Telr Gateway Settings")
		settings.treat_authorised_as_paid = treat_authorised_as_paid
		with patch.object(type(settings), "get_order", return_value=order):
			return settings.get_payment_status("telr_order_ref")

	def test_an_authorised_order_stays_pending_until_the_store_says_otherwise(self):
		"""An authorise-only store settles later; calling this Paid ships goods against uncaptured funds."""
		self.assertEqual(self.get_payment_status(AUTHORISED_ORDER, 0), "Pending")

	def test_an_authorised_order_counts_as_paid_once_the_store_is_marked_auth_and_capture(self):
		"""Telr `ecom` authorises and captures together, so these orders otherwise sit unfulfilled forever."""
		self.assertEqual(self.get_payment_status(AUTHORISED_ORDER, 1), "Paid")

	def test_the_american_spelling_is_mapped_the_same_way(self):
		order = {"status": {"text": "authorized"}}

		self.assertEqual(self.get_payment_status(order, 1), "Paid")
		self.assertEqual(self.get_payment_status(order, 0), "Pending")

	def test_the_switch_never_promotes_a_status_telr_did_not_authorise(self):
		self.assertEqual(self.get_payment_status({"status": {"text": "Declined"}}, 1), "Not Paid")
		self.assertEqual(self.get_payment_status({"status": {"text": "Cancelled"}}, 1), "Cancelled")
		# Anything unmapped stays Pending: never terminal, and never Paid.
		self.assertEqual(self.get_payment_status({"status": {"text": "Who Knows"}}, 1), "Pending")
