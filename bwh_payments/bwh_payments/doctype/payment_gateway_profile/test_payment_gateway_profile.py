# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.bwh_payments.utils import get_available_payment_modes

IGNORE_TEST_RECORD_DEPENDENCIES = ["Address", "Company", "Customer", "Payment Request"]


def make_profile(name: str, enabled: int = 1):
	return frappe.get_doc(
		{
			"doctype": "Payment Gateway Profile",
			"name": name,
			"gateway_settings": "Stripe Gateway Settings",
			"enabled": enabled,
		}
	).insert(ignore_permissions=True)


class TestPaymentGatewayProfile(IntegrationTestCase):
	def test_a_disabled_profile_is_no_longer_offered(self):
		profile = make_profile("Profile Cache Disable")
		self.assertIn(profile.name, get_available_payment_modes())

		profile.enabled = 0
		profile.save(ignore_permissions=True)

		self.assertNotIn(profile.name, get_available_payment_modes())

	def test_a_renamed_profile_is_offered_under_its_new_name_only(self):
		profile = make_profile("Profile Cache Old Name")
		self.assertIn(profile.name, get_available_payment_modes())

		frappe.rename_doc("Payment Gateway Profile", profile.name, "Profile Cache New Name", force=True)

		offered = get_available_payment_modes()
		self.assertIn("Profile Cache New Name", offered)
		self.assertNotIn("Profile Cache Old Name", offered)

	def test_the_offered_list_is_shared_by_every_worker(self):
		make_profile("Profile Cache Shared")
		get_available_payment_modes()

		cache_key = f"{get_available_payment_modes.__module__}.get_available_payment_modes"
		self.assertTrue(frappe.cache.get_keys(cache_key))
