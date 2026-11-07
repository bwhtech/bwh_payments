# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from pydantic import ValidationError

from bwh_payments import api
from bwh_payments.bwh_payments.doctype.gateway_linked_account.gateway_linked_account import (
	sync_unsettled_accounts,
)
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	GATEWAY as STRIPE_GATEWAY,
)
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	configure_stripe_gateway,
	remove_stripe_gateway,
)
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RazorpayTestCase,
)
from bwh_payments.services.razorpay.stub import StubRazorpayClient
from bwh_payments.testing import without_commits

IGNORE_TEST_RECORD_DEPENDENCIES = ["Address", "Company", "Customer", "Payment Request"]

BANK_ACCOUNT_NUMBER = "50100123456789"


def onboarding_details(**overrides) -> dict:
	return {
		"email": "owner@team.test",
		"phone": "9000090000",
		"legal_business_name": "Acme Events LLP",
		"business_type": "llp",
		"contact_name": "Gaurav Kumar",
		"category": "media_and_entertainment",
		"subcategory": "ticketing",
		"registered_address": {
			"street1": "507, Koramangala 1st block",
			"city": "Bengaluru",
			"state": "KARNATAKA",
			"postal_code": "560034",
		},
		"business_pan": "AAACL1234C",
		"stakeholder_name": "Gaurav Kumar",
		"stakeholder_email": "owner@team.test",
		"bank_account_number": BANK_ACCOUNT_NUMBER,
		"ifsc_code": "HDFC0000317",
		"beneficiary_name": "Acme Events LLP",
		"terms_accepted": True,
		**overrides,
	}


def submit(owner: str = "INR", **overrides):
	return api.submit_linked_account(RAZORPAY_GATEWAY, "Currency", owner, onboarding_details(**overrides))


def product_id(account) -> str:
	return frappe.parse_json(account.gateway_reference)["product_id"]


class TestGatewayLinkedAccount(RazorpayTestCase):
	def setUp(self):
		super().setUp()
		# Registered first so it runs last: onboarding commits, and each test starts from no account.
		self.addCleanup(frappe.db.rollback)
		self.enterContext(without_commits())

	def calls(self) -> list[str]:
		return [call[0] for call in StubRazorpayClient.account_calls]

	def test_submitting_opens_the_account_step_by_step(self):
		account = submit()

		self.assertEqual(
			self.calls(), ["create_account", "create_stakeholder", "request_product", "update_product"]
		)
		self.assertTrue(account.account_id.startswith("acc_"))
		self.assertEqual(account.status, "Pending")
		self.assertEqual(account.gateway_status, "under_review")
		create_account = StubRazorpayClient.account_calls[0][2]
		self.assertEqual((create_account["type"], create_account["reference_id"]), ("route", account.name))

	def test_the_bank_details_reach_razorpay_and_are_not_stored(self):
		account = submit()

		settlements = StubRazorpayClient.account_calls[-1][2]["settlements"]
		self.assertEqual(settlements["account_number"], BANK_ACCOUNT_NUMBER)
		self.assertNotIn(BANK_ACCOUNT_NUMBER, frappe.as_json(account.as_dict()))

	def test_submitting_again_updates_what_razorpay_holds(self):
		first = submit()
		StubRazorpayClient.account_calls.clear()

		second = submit(ifsc_code="ICIC0000001")

		self.assertEqual(second.name, first.name)
		self.assertEqual(self.calls(), ["update_account", "update_stakeholder", "update_product"])
		self.assertNotIn("email", StubRazorpayClient.account_calls[0][2])

	def test_a_failure_halfway_keeps_the_account_and_resumes_from_there(self):
		with patch.object(StubRazorpayClient, "create_stakeholder", side_effect=frappe.ValidationError):
			with self.assertRaises(frappe.ValidationError):
				submit()
		StubRazorpayClient.account_calls.clear()

		account = submit()

		self.assertEqual(
			self.calls(), ["update_account", "create_stakeholder", "request_product", "update_product"]
		)
		self.assertEqual(frappe.db.count("Gateway Linked Account", {"ref_docname": "INR"}), 1)
		self.assertTrue(account.account_id)

	def test_the_owner_must_accept_razorpays_terms(self):
		with self.assertRaises(ValidationError):
			submit(terms_accepted=False)

		self.assertEqual(StubRazorpayClient.account_calls, [])

	def test_an_activated_account_becomes_active(self):
		account = submit()
		StubRazorpayClient.set_activation_status(product_id(account), "activated")

		api.sync_linked_account(account.name)

		self.assertEqual(frappe.db.get_value("Gateway Linked Account", account.name, "status"), "Active")
		self.assertEqual(api.get_linked_account(RAZORPAY_GATEWAY, "Currency", "INR"), account.name)

	def test_what_razorpay_still_needs_is_kept(self):
		account = submit()
		requirements = [{"field_reference": "settlements.ifsc_code", "reason_code": "field_missing"}]
		StubRazorpayClient.set_activation_status(product_id(account), "needs_clarification", requirements)

		sync_unsettled_accounts()

		account.reload()
		self.assertEqual(account.status, "Needs Clarification")
		self.assertEqual(frappe.parse_json(account.requirements), requirements)
		self.assertIsNone(api.get_linked_account(RAZORPAY_GATEWAY, "Currency", "INR"))

	def test_an_account_added_by_hand_keeps_the_status_it_was_given(self):
		account = make_account(status="Active")

		account.update_from_gateway()

		self.assertEqual(account.status, "Active")

	def test_an_owner_has_one_active_account_per_gateway(self):
		make_account(status="Active")

		with self.assertRaisesRegex(frappe.ValidationError, "already has an active linked account"):
			make_account(status="Active")


class TestLinkedAccountGateways(RazorpayTestCase):
	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		remove_stripe_gateway()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	def test_a_gateway_that_cannot_split_cannot_hold_linked_accounts(self):
		configure_stripe_gateway()

		with self.assertRaisesRegex(frappe.ValidationError, "cannot send payments on to linked accounts"):
			make_account(gateway=STRIPE_GATEWAY)


def make_account(status: str = "Pending", gateway: str = RAZORPAY_GATEWAY):
	return frappe.get_doc(
		{
			"doctype": "Gateway Linked Account",
			"gateway": gateway,
			"account_id": f"acc_{frappe.generate_hash(length=12)}",
			"status": status,
			"ref_doctype": "Currency",
			"ref_docname": "INR",
		}
	).insert(ignore_permissions=True)
