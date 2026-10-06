# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.deferred_insert import save_to_db as save_deferred_inserts
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from bwh_payments.bwh_payments.doctype.stripe_gateway_settings import stripe_gateway_settings
from bwh_payments.currency import to_minor_units
from bwh_payments.tests.fake_stripe import FakeRefundService, FakeStripeClient

GATEWAY = "Stripe Test Gateway"
WEBHOOK_SECRET = "whsec_test_secret"

# None of these links are exercised here, and generating their fixtures drags in ERPNext's whole test
# bootstrap for no benefit.
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
	"""Undo configure_stripe_gateway.

	Callers reach for this from setUpClass, which runs outside the per-test rollback. Without it a test
	run leaves the dev site with an enabled gateway backed by a fake `sk_test_x` key, which the storefront
	then offers shoppers at checkout.

	The requests have to go too: the refund lock tests commit theirs so a second connection can see it, which
	pins that Gateway Payment Request to the site for good.
	"""
	for request_name in frappe.get_all("Gateway Payment Request", filters={"gateway": GATEWAY}, pluck="name"):
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	frappe.delete_doc(
		"Payment Gateway Profile", GATEWAY, ignore_missing=True, ignore_permissions=True, force=True
	)
	# set_single_value, not save(): the keys are mandatory, so blanking them cannot go through validation.
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
		FakeStripeClient.reset()
		self.stripe_client_patch = patch.object(
			stripe_gateway_settings.stripe, "StripeClient", FakeStripeClient
		)
		self.stripe_client_patch.start()
		self.addCleanup(self.stripe_client_patch.stop)
		configure_stripe_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Roll back the suite's own writes first so the commit below persists nothing but the cleanup, and
		# commit it because the class-level rollback frappe queues after this would otherwise undo it.
		frappe.db.rollback()
		remove_stripe_gateway()
		frappe.db.commit()

	def use_currency_number_format(self):
		"""Let Currency fields take their precision from the currency, not the site number format."""
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 1)
		frappe.clear_cache()

	def mark_paid(self, payment_request):
		FakeStripeClient.register_paid_session(
			payment_request.order_ref, payment_request.currency_code.lower()
		)
		payment_request.db_set("status", "Paid", update_modified=False)
		payment_request.reload()

	# --- session creation -------------------------------------------------

	def test_create_session_charges_iso_minor_units_for_a_three_decimal_currency(self):
		"""KWD has three decimals. The ported int(amount * 100) billed 1234 fils for 12.345 KWD."""
		self.use_currency_number_format()
		payment_request = make_payment_request(12.345, "KWD")

		charged = FakeStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.assertEqual(charged, 12345)
		self.assertNotEqual(charged, int(12.345 * 100))
		self.assertTrue(payment_request.order_ref)

	def test_a_three_decimal_amount_is_refused_when_the_site_stores_only_two(self):
		"""Better to refuse the charge than to store 12.35 KWD and bill the shopper a different figure."""
		frappe.db.set_single_value("System Settings", "use_number_format_from_currency", 0)
		frappe.clear_cache()

		with self.assertRaises(frappe.ValidationError):
			make_payment_request(12.345, "KWD")

		self.assertEqual(FakeStripeClient.created_sessions, [])

	def test_create_session_charges_iso_minor_units_for_a_zero_decimal_currency(self):
		make_payment_request(1000, "JPY")

		charged = FakeStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
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

	# --- refund ledger ----------------------------------------------------

	def test_charge_and_refund_agree_on_the_minor_unit_conversion(self):
		self.use_currency_number_format()
		payment_request = make_payment_request(12.345, "KWD")
		charged = FakeStripeClient.created_sessions[-1]["line_items"][0]["price_data"]["unit_amount"]
		self.mark_paid(payment_request)

		payment_request.refund()

		self.assertEqual(FakeStripeClient.created_refunds[-1]["amount"], charged)
		self.assertEqual(to_minor_units(payment_request.refund_amount, "KWD"), charged)
		self.assertEqual(payment_request.status, "Refunded")

	def test_refund_guard_reads_the_locked_ledger_not_the_in_memory_copy(self):
		"""A stale in-memory refund_amount is exactly how two concurrent refunds both pass the guard."""
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		# Stand in for a refund committed by another request after this document was loaded.
		frappe.db.set_value(
			"Gateway Payment Request", payment_request.name, "refund_amount", 100, update_modified=False
		)
		self.assertEqual(flt(payment_request.refund_amount), 0.0)

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(100)

		self.assertEqual(FakeStripeClient.created_refunds, [])

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
		FakeStripeClient.created_refunds.clear()

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(41)

		self.assertEqual(FakeStripeClient.created_refunds, [])
		self.assertEqual(flt(payment_request.refund_amount), 60.0)

	def test_an_omitted_amount_refunds_the_whole_remaining_balance(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		payment_request.refund(40)

		payment_request.refund()

		self.assertEqual(flt(payment_request.refund_amount), 100.0)
		self.assertEqual(payment_request.status, "Refunded")

	def test_a_sub_minor_unit_amount_is_rejected_rather_than_refunding_everything(self):
		"""`flt(amount, 2) or remaining` made flt(0.004, 2) == 0.0 falsy, so 0.004 refunded the lot."""
		payment_request = make_payment_request(500, "SAR")
		self.mark_paid(payment_request)

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(0.004)

		self.assertEqual(FakeStripeClient.created_refunds, [])
		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 0.0)
		self.assertEqual(payment_request.status, "Paid")

	def test_an_explicit_zero_is_rejected_rather_than_refunding_everything(self):
		payment_request = make_payment_request(500, "SAR")
		self.mark_paid(payment_request)

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(0)

		self.assertEqual(FakeStripeClient.created_refunds, [])
		self.assertEqual(flt(payment_request.refund_amount), 0.0)

	def test_a_residue_under_a_whole_unit_stays_refundable(self):
		"""Relabelling this Refunded drops it out of REFUNDABLE_STATUSES and strands the 0.99 forever."""
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		payment_request.refund(99.01)
		self.assertEqual(payment_request.status, "Partially Refunded")

		payment_request.refund()

		self.assertEqual(flt(payment_request.refund_amount), 100.0)
		self.assertEqual(payment_request.status, "Refunded")
		self.assertEqual(FakeStripeClient.created_refunds[-1]["amount"], to_minor_units(0.99, "SAR"))

	def test_a_whole_unit_left_is_still_only_partially_refunded(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)

		payment_request.refund(99)

		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_refund_rejected_while_the_payment_is_still_pending(self):
		payment_request = make_payment_request(100, "SAR")

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(10)

		self.assertEqual(FakeStripeClient.created_refunds, [])

	def test_a_failed_gateway_refund_leaves_the_ledger_untouched(self):
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		FakeStripeClient.next_refund_status = "failed"

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(50)

		payment_request.reload()
		self.assertEqual(flt(payment_request.refund_amount), 0.0)
		self.assertIsNone(payment_request.refund_id)

	def test_the_ledger_stays_locked_while_the_gateway_refunds(self):
		"""A second refund has to wait for this one. Once the row lock is released before the gateway call,
		a concurrent refund reads the old refund_amount, passes the over-refund guard and refunds again."""
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		# Committed so the second connection can see the row; the lock is then the only thing in its way.
		frappe.db.commit()  # nosemgrep

		lock_held_during_refund = []
		create_refund = FakeRefundService.create

		def create_refund_while_probing_the_lock(service, params):
			lock_held_during_refund.append(not self.can_lock_from_another_connection(payment_request.name))
			return create_refund(service, params)

		with patch.object(FakeRefundService, "create", create_refund_while_probing_the_lock):
			payment_request.refund(40)

		self.assertEqual(lock_held_during_refund, [True])

	def test_a_failed_gateway_refund_is_recorded_after_the_rollback(self):
		"""The failure is the record an operator reconciles against, so it has to outlive the rollback that
		the error triggers. It used to stay Queued, because the Failed update was rolled back."""
		payment_request = make_payment_request(100, "SAR")
		self.mark_paid(payment_request)
		# Committed so the request outlives the rollback below; the deferred Failed log links to it.
		frappe.db.commit()  # nosemgrep
		FakeStripeClient.next_refund_status = "failed"

		with self.assertRaises(frappe.ValidationError):
			payment_request.refund(50)
		# What the request handler does with the error.
		frappe.db.rollback()
		save_deferred_inserts("Integration Request")

		statuses = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": f"{GATEWAY} Refund",
				"reference_docname": payment_request.name,
			},
			pluck="status",
		)
		self.assertEqual(statuses, ["Failed"])

	def can_lock_from_another_connection(self, payment_request_name: str) -> bool:
		# On its first use secondary_connection() captures the connection to restore *after* opening the
		# second one, so it leaves the second one active. Put the refund back on its own connection.
		primary = frappe.local.db
		try:
			with self.secondary_connection():
				try:
					frappe.db.get_value(
						"Gateway Payment Request", payment_request_name, "name", for_update=True, wait=False
					)
				except frappe.QueryTimeoutError:
					return False
				finally:
					# Never hold the lock past the probe: the refund under test is waiting to save this row.
					frappe.db.rollback()
			return True
		finally:
			frappe.local.db = primary

	# --- polled status ----------------------------------------------------

	def test_an_abandoned_session_is_released_and_expired_at_the_gateway(self):
		payment_request = make_payment_request(100, "SAR")

		self.assertTrue(payment_request.release_if_unpaid())
		self.assertEqual(payment_request.status, "Cancelled")
		self.assertEqual(FakeStripeClient.sessions[payment_request.order_ref]["status"], "expired")

	def test_a_paid_session_keeps_its_payment_and_is_never_expired(self):
		payment_request = make_payment_request(100, "SAR")
		FakeStripeClient.register_paid_session(payment_request.order_ref, "sar")

		self.assertFalse(payment_request.release_if_unpaid())
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(FakeStripeClient.sessions[payment_request.order_ref]["status"], "complete")

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
		"""The shopper's poll and the webhook race; the poll must lose, not overwrite."""
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

	# --- webhook status ---------------------------------------------------

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

	# --- schema invariants ------------------------------------------------

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
		# A submittable refund ledger can be amended into a second copy and double-count refunds.
		self.assertFalse(frappe.get_meta("Gateway Payment Request").is_submittable)
