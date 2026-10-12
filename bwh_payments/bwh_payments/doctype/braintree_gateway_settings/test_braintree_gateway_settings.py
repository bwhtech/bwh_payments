# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from bwh_payments.api import get_gateways
from bwh_payments.bwh_payments.doctype.braintree_gateway_settings import braintree_gateway_settings
from bwh_payments.services.braintree.stub import StubBraintreeClient

BRAINTREE_GATEWAY = "Braintree"
USD_ACCOUNT = "usd_account"
AUD_ACCOUNT = "aud_account"
SUCCESS_URL = "https://shop.test/en/account/orders/confirmation"

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_braintree_gateway():
	StubBraintreeClient.add_merchant_account(USD_ACCOUNT)
	StubBraintreeClient.add_merchant_account(AUD_ACCOUNT)
	settings = frappe.get_single("Braintree Gateway Settings")
	settings.update(
		{
			"enabled": 1,
			"mode": "Sandbox",
			"merchant_id": "test_merchant",
			"public_key": "test_public_key",
			"private_key": "test_private_key",
			"three_d_secure": 1,
			"merchant_accounts": [],
			"success_url": SUCCESS_URL,
			"cancelled_url": "https://shop.test/en/cart",
			"failure_url": "https://shop.test/en/cart/checkout",
		}
	)
	settings.append("merchant_accounts", {"currency": "USD", "merchant_account_id": USD_ACCOUNT})
	settings.append("merchant_accounts", {"currency": "AUD", "merchant_account_id": AUD_ACCOUNT})
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", BRAINTREE_GATEWAY):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": BRAINTREE_GATEWAY,
				"gateway_settings": "Braintree Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def remove_braintree_gateway():
	"""`pay()` and `create_request_log` commit, so these tests escape the rollback and need this cleanup."""
	frappe.db.delete(
		"Gateway Refund",
		{
			"payment_request": [
				"in",
				frappe.get_all("Gateway Payment Request", {"gateway": BRAINTREE_GATEWAY}, pluck="name"),
			]
		},
	)
	for request_name in frappe.get_all(
		"Gateway Payment Request", filters={"gateway": BRAINTREE_GATEWAY}, pluck="name"
	):
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	for service in ("Braintree", f"{BRAINTREE_GATEWAY} Webhook"):
		for log_name in frappe.get_all(
			"Integration Request", filters={"integration_request_service": service}, pluck="name"
		):
			frappe.delete_doc(
				"Integration Request", log_name, ignore_permissions=True, delete_permanently=True
			)

	frappe.db.delete("Error Log", {"method": ["like", "Braintree order placement failed%"]})
	frappe.delete_doc(
		"Payment Gateway Profile",
		BRAINTREE_GATEWAY,
		ignore_missing=True,
		ignore_permissions=True,
		force=True,
	)
	# Not save(): the keys are mandatory.
	frappe.db.set_single_value(
		"Braintree Gateway Settings",
		{
			"enabled": 0,
			"merchant_id": "",
			"public_key": "",
			"private_key": "",
			"success_url": "",
			"cancelled_url": "",
			"failure_url": "",
		},
	)
	frappe.db.delete("Braintree Merchant Account", {"parent": "Braintree Gateway Settings"})
	frappe.clear_cache(doctype="Braintree Gateway Settings")


def make_braintree_payment_request(amount: float, currency: str = "USD"):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": BRAINTREE_GATEWAY,
			"amount": amount,
			"currency_code": currency,
			"ref_doctype": "Currency",
			"ref_docname": currency,
		}
	).insert(ignore_permissions=True)


class BraintreeTestCase(IntegrationTestCase):
	def setUp(self):
		StubBraintreeClient.reset()
		configure_braintree_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Commit only the cleanup; frappe's class-level rollback would undo it otherwise.
		frappe.db.rollback()
		remove_braintree_gateway()
		frappe.db.commit()  # nosemgrep

	def get_settings(self):
		return frappe.get_single("Braintree Gateway Settings")


class TestBraintreeGatewaySettings(BraintreeTestCase):
	def test_an_unknown_merchant_account_cannot_be_enabled(self):
		settings = self.get_settings()
		settings.append("merchant_accounts", {"currency": "EUR", "merchant_account_id": "missing"})

		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_a_currency_can_have_only_one_merchant_account(self):
		settings = self.get_settings()
		settings.append("merchant_accounts", {"currency": "USD", "merchant_account_id": AUD_ACCOUNT})

		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_enabling_without_a_merchant_account_is_refused(self):
		settings = self.get_settings()
		settings.merchant_accounts = []

		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_declares_the_currencies_it_has_merchant_accounts_for(self):
		self.assertEqual(self.get_settings().supported_currencies(), ("USD", "AUD"))

	def test_refuses_a_currency_without_a_merchant_account(self):
		with self.assertRaises(frappe.ValidationError):
			make_braintree_payment_request(100, "EUR")

	def test_session_is_a_random_token_and_sends_the_shopper_to_our_page(self):
		payment_request = make_braintree_payment_request(20)

		self.assertEqual(len(payment_request.order_ref), 32)
		self.assertNotEqual(payment_request.order_ref, payment_request.name)
		self.assertIn(f"/braintree-checkout?session={payment_request.order_ref}", payment_request.order_url)
		self.assertIn(f"reference_id={payment_request.name}", payment_request.success_url)
		self.assertIn(f"reference_id={payment_request.name}", payment_request.cancel_url)

	def test_missing_success_url_is_refused(self):
		settings = self.get_settings()
		with self.assertRaises(frappe.ValidationError):
			settings.build_return_url("", "Success URL", "GPR-0001")

	def test_charges_through_the_currency_merchant_account_at_its_precision(self):
		settings = self.get_settings()
		for currency, amount, account, sent in (
			("USD", 12.5, USD_ACCOUNT, "12.50"),
			("AUD", 7, AUD_ACCOUNT, "7.00"),
		):
			with self.subTest(currency=currency):
				settings.charge("session", amount, currency, "fake-valid-nonce")
				sale = StubBraintreeClient.sales[-1]
				self.assertEqual(sale["amount"], sent)
				self.assertEqual(sale["merchant_account_id"], account)
				self.assertEqual(sale["options"], {"submit_for_settlement": True})

	def test_amounts_follow_the_currency_exponent(self):
		self.assertEqual(braintree_gateway_settings.format_amount(1000, "JPY"), "1000")
		self.assertEqual(braintree_gateway_settings.format_amount(1.234, "KWD"), "1.234")

	def test_status_map(self):
		settings = self.get_settings()
		for braintree_status, expected in (
			("submitted_for_settlement", "Paid"),
			("settling", "Paid"),
			("settled", "Paid"),
			("authorized", "Pending"),
			("processor_declined", "Pending"),
			("gateway_rejected", "Pending"),
			("settlement_declined", "Not Paid"),
			("voided", "Cancelled"),
			("something_new", "Pending"),
		):
			with self.subTest(braintree_status=braintree_status):
				session = frappe.generate_hash(length=32)
				StubBraintreeClient.add_transaction(session, status=braintree_status)
				self.assertEqual(settings.get_payment_status(session), expected)

	def test_no_transaction_yet_is_pending(self):
		self.assertEqual(self.get_settings().get_payment_status("nothing-here"), "Pending")

	def test_a_retry_after_a_decline_counts_as_paid(self):
		session = frappe.generate_hash(length=32)
		StubBraintreeClient.add_transaction(session, status="processor_declined")
		StubBraintreeClient.add_transaction(session, status="submitted_for_settlement")

		self.assertEqual(self.get_settings().get_payment_status(session), "Paid")

	def test_a_refund_credit_is_never_read_as_the_payment(self):
		session = frappe.generate_hash(length=32)
		sale_id = StubBraintreeClient.add_transaction(session, status="voided")
		StubBraintreeClient.add_transaction(session, status="submitted_for_settlement", type="credit")

		self.assertEqual(self.get_settings().get_payment_status(session), "Cancelled")
		self.assertIn(sale_id, StubBraintreeClient.transactions)

	def test_a_fresh_sale_missing_from_search_is_found_through_the_request(self):
		payment_request = make_braintree_payment_request(20)
		sale_id = StubBraintreeClient.add_transaction(payment_request.order_ref)
		StubBraintreeClient.unindexed.add(sale_id)
		payment_request.db_set("gateway_transaction_ref", sale_id)

		self.assertEqual(self.get_settings().get_payment_status(payment_request.order_ref), "Paid")

	def test_a_settled_sale_takes_partial_refunds_up_to_its_amount(self):
		settings = self.get_settings()
		session = frappe.generate_hash(length=32)
		sale_id = StubBraintreeClient.add_transaction(session, amount="10.00")
		StubBraintreeClient.settle(sale_id)

		first = settings.refund_payment(session, 4, "USD")
		settings.refund_payment(session, 6, "USD")

		self.assertEqual(first.amount, 4)
		self.assertEqual(
			StubBraintreeClient.refunds,
			[{"transaction_id": sale_id, "amount": "4.00"}, {"transaction_id": sale_id, "amount": "6.00"}],
		)
		with self.assertRaises(frappe.ValidationError):
			settings.refund_payment(session, 1, "USD")

	def test_an_unsettled_sale_is_voided_for_a_full_refund(self):
		settings = self.get_settings()
		session = frappe.generate_hash(length=32)
		sale_id = StubBraintreeClient.add_transaction(session, amount="10.00")

		result = settings.refund_payment(session, 10, "USD")

		self.assertEqual(StubBraintreeClient.voids, [sale_id])
		# A void is the full refund, so Gateway Refund records it as Processed.
		self.assertEqual(result.status, "processed")
		self.assertEqual(result.amount, 10)

	def test_an_unsettled_sale_refuses_a_partial_refund(self):
		settings = self.get_settings()
		session = frappe.generate_hash(length=32)
		StubBraintreeClient.add_transaction(session, amount="10.00")

		with self.assertRaises(frappe.ValidationError):
			settings.refund_payment(session, 4, "USD")
		self.assertEqual(StubBraintreeClient.voids, [])
		self.assertEqual(StubBraintreeClient.refunds, [])

	def test_nothing_paid_is_never_refunded(self):
		session = frappe.generate_hash(length=32)
		StubBraintreeClient.add_transaction(session, status="processor_declined")

		with self.assertRaises(frappe.ValidationError):
			self.get_settings().refund_payment(session, 5, "USD")
		self.assertEqual(StubBraintreeClient.refunds, [])

	def test_release_if_unpaid_cancels_an_open_session(self):
		payment_request = make_braintree_payment_request(20)

		self.assertTrue(payment_request.release_if_unpaid())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Cancelled")

	def test_release_if_unpaid_refuses_a_paid_session(self):
		payment_request = make_braintree_payment_request(20)
		StubBraintreeClient.add_transaction(payment_request.order_ref)

		self.assertFalse(payment_request.release_if_unpaid())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_settled_refund_is_recorded_as_processed_through_the_request(self):
		payment_request = make_braintree_payment_request(20)
		sale_id = StubBraintreeClient.add_transaction(payment_request.order_ref, amount="20.00")
		StubBraintreeClient.settle(sale_id)
		payment_request.sync_status()

		refund = payment_request.create_refund(5)

		self.assertEqual(refund.status, "Processed")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_an_unsettled_full_refund_voids_and_refunds_the_request(self):
		payment_request = make_braintree_payment_request(20)
		StubBraintreeClient.add_transaction(payment_request.order_ref, amount="20.00")
		payment_request.sync_status()

		refund = payment_request.create_refund()

		self.assertEqual(refund.status, "Processed")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Refunded")

	def test_refund_status_is_read_back_from_braintree(self):
		settings = self.get_settings()
		session = frappe.generate_hash(length=32)
		sale_id = StubBraintreeClient.add_transaction(session, amount="10.00")
		StubBraintreeClient.settle(sale_id)
		refund_id = settings.refund_payment(session, 4, "USD").refund_id

		StubBraintreeClient.set_status(refund_id, "settlement_declined")

		self.assertEqual(settings.get_refund_status(session, refund_id).status, "failed")

	def test_the_transaction_ref_is_the_paid_sale(self):
		settings = self.get_settings()
		session = frappe.generate_hash(length=32)
		StubBraintreeClient.add_transaction(session, status="processor_declined")
		sale_id = StubBraintreeClient.add_transaction(session)

		self.assertEqual(settings.get_transaction_ref(session), sale_id)
		self.assertIsNone(settings.get_transaction_ref("nothing-paid"))

	def test_a_return_url_replaces_every_way_back(self):
		session = self.get_settings().create_session(
			20, "USD", reference="GPR-1", return_url="https://shop.test/r"
		)

		self.assertEqual(
			(session.success_url, session.cancel_url, session.failure_url), ("https://shop.test/r",) * 3
		)

	def test_braintree_is_listed_with_a_reusable_checkout(self):
		gateway = next(gateway for gateway in get_gateways() if gateway.name == BRAINTREE_GATEWAY)

		self.assertEqual(gateway.settings_doctype, "Braintree Gateway Settings")
		self.assertTrue(gateway.reusable_checkout)
