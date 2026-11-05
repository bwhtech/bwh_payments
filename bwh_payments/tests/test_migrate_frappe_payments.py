# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import unittest

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from bwh_payments.migrate.frappe_payments import import_gateways


def frappe_payments_is_installed() -> bool:
	return "payments" in frappe.get_installed_apps()


@unittest.skipUnless(frappe_payments_is_installed(), "frappe/payments is not installed on this site")
class TestImportFromFrappePayments(IntegrationTestCase):
	def setUp(self):
		for name in ("Razorpay", "Stripe-Legacy", "Stripe-Second", "PayPal"):
			frappe.delete_doc("Payment Gateway Profile", name, ignore_missing=True, force=True)
			frappe.db.delete("Payment Gateway", {"name": name})
		frappe.db.delete("Stripe Settings", {"name": ["in", ["Legacy", "Second"]]})
		frappe.db.set_single_value("Razorpay Gateway Settings", {"key_id": "", "webhook_secret": ""})
		frappe.db.set_single_value("Stripe Gateway Settings", {"public_key": "", "private_key": ""})

		# Inserted raw: the frappe/payments controllers call the gateway to check credentials on save.
		# frappe/payments creates its Razorpay gateway by name only, with no gateway_settings.
		self.add_payment_gateway("Razorpay")
		frappe.db.set_single_value("Razorpay Settings", "api_key", "rzp_test_legacy")
		set_encrypted_password("Razorpay Settings", "Razorpay Settings", "legacy_key_secret", "api_secret")
		set_encrypted_password(
			"Razorpay Settings", "Razorpay Settings", "legacy_webhook_secret", "webhook_secret"
		)

		self.add_stripe_account("Legacy", "pk_test_legacy", "sk_test_legacy")
		self.add_stripe_account("Second", "pk_test_second", "sk_test_second")
		self.add_payment_gateway("PayPal", "PayPal Settings")

	def add_payment_gateway(self, name: str, settings: str | None = None, controller: str | None = None):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": name,
				"gateway_settings": settings,
				"gateway_controller": controller,
			}
		).db_insert()

	def add_stripe_account(self, name: str, publishable_key: str, secret_key: str):
		frappe.get_doc(
			{
				"doctype": "Stripe Settings",
				"name": name,
				"gateway_name": name,
				"publishable_key": publishable_key,
			}
		).db_insert()
		set_encrypted_password("Stripe Settings", name, secret_key, "secret_key")
		self.add_payment_gateway(f"Stripe-{name}", "Stripe Settings", name)

	def get_profile(self, name: str):
		return frappe.db.get_value(
			"Payment Gateway Profile", name, ["enabled", "gateway_settings"], as_dict=True
		)

	def test_razorpay_keeps_its_name_and_its_keys(self):
		import_gateways()

		self.assertEqual(
			self.get_profile("Razorpay"), {"enabled": 1, "gateway_settings": "Razorpay Gateway Settings"}
		)
		self.assertEqual(frappe.db.get_single_value("Razorpay Gateway Settings", "key_id"), "rzp_test_legacy")
		self.assertEqual(
			get_decrypted_password("Razorpay Gateway Settings", "Razorpay Gateway Settings", "key_secret"),
			"legacy_key_secret",
		)
		self.assertEqual(
			get_decrypted_password(
				"Razorpay Gateway Settings", "Razorpay Gateway Settings", "webhook_secret"
			),
			"legacy_webhook_secret",
		)

	def test_the_first_stripe_account_moves_and_any_other_is_kept_for_history_only(self):
		import_gateways()

		self.assertEqual(
			self.get_profile("Stripe-Legacy"), {"enabled": 1, "gateway_settings": "Stripe Gateway Settings"}
		)
		self.assertEqual(self.get_profile("Stripe-Second"), {"enabled": 0, "gateway_settings": None})
		settings = frappe.get_single("Stripe Gateway Settings")
		self.assertEqual((settings.public_key, settings.mode), ("pk_test_legacy", "Test"))
		self.assertEqual(settings.get_password("private_key"), "sk_test_legacy")

	def test_a_gateway_this_app_cannot_run_is_kept_for_history_but_disabled(self):
		import_gateways()

		self.assertEqual(self.get_profile("PayPal"), {"enabled": 0, "gateway_settings": None})

	def test_a_profile_left_without_settings_gets_its_keys_on_the_next_run(self):
		frappe.get_doc({"doctype": "Payment Gateway Profile", "name": "Razorpay", "enabled": 0}).insert()

		self.assertIn("Razorpay", import_gateways())
		self.assertEqual(
			self.get_profile("Razorpay"), {"enabled": 1, "gateway_settings": "Razorpay Gateway Settings"}
		)
		self.assertEqual(frappe.db.get_single_value("Razorpay Gateway Settings", "key_id"), "rzp_test_legacy")

	def test_running_the_import_again_changes_nothing(self):
		first = import_gateways()
		frappe.db.set_single_value("Razorpay Gateway Settings", "key_id", "rzp_test_reconfigured")

		self.assertEqual(import_gateways(), [])
		self.assertIn("Razorpay", first)
		self.assertEqual(
			frappe.db.get_single_value("Razorpay Gateway Settings", "key_id"), "rzp_test_reconfigured"
		)
