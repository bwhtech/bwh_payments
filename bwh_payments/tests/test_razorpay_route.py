from unittest.mock import patch

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

from bwh_payments import api
from bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request import (
	get_requests_with_open_splits,
	sync_open_split_transfers,
	to_aware_datetime,
)
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RazorpayTestCase,
	configure_razorpay_gateway,
)
from bwh_payments.bwh_payments.utils import run_as_administrator
from bwh_payments.services.razorpay.stub import StubRazorpayClient


def make_linked_account(owner: str = "INR", status: str = "Active") -> str:
	"""A linked account paying out to a Currency record, which stands in for a team."""
	return (
		frappe.get_doc(
			{
				"doctype": "Gateway Linked Account",
				"gateway": RAZORPAY_GATEWAY,
				"account_id": f"acc_{frappe.generate_hash(length=12)}",
				"status": status,
				"ref_doctype": "Currency",
				"ref_docname": owner,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def make_split_payment(amount: float, splits: list[dict], currency: str = "INR"):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": RAZORPAY_GATEWAY,
			"amount": amount,
			"currency_code": currency,
			"ref_doctype": "Currency",
			"ref_docname": currency,
			"splits": splits,
		}
	).insert(ignore_permissions=True)


def pay(payment_request):
	StubRazorpayClient.pay(payment_request.order_ref)
	with run_as_administrator():
		payment_request.sync_status()
	payment_request.reload()


def account_id(linked_account: str) -> str:
	return frappe.db.get_value("Gateway Linked Account", linked_account, "account_id")


class RazorpaySplitTestCase(RazorpayTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		configure_razorpay_gateway()
		cls.account = make_linked_account()


class TestRazorpaySplits(RazorpaySplitTestCase):
	def setUp(self):
		super().setUp()
		self.hold_until = add_days(now_datetime(), 10).replace(microsecond=0)

	def test_a_split_payment_sends_its_transfer_with_the_hold(self):
		make_split_payment(
			1000, [{"linked_account": self.account, "amount": 900, "hold_until": self.hold_until}]
		)

		self.assertEqual(
			StubRazorpayClient.created_links[-1]["options"]["order"]["transfers"],
			[
				{
					"account": account_id(self.account),
					"amount": 90000,
					"currency": "INR",
					"on_hold": True,
					"on_hold_until": int(to_aware_datetime(self.hold_until).timestamp()),
				}
			],
		)

	def test_a_hold_that_has_passed_sends_the_share_without_a_hold(self):
		make_split_payment(
			1000,
			[{"linked_account": self.account, "amount": 900, "hold_until": add_days(now_datetime(), -1)}],
		)

		transfer = StubRazorpayClient.created_links[-1]["options"]["order"]["transfers"][0]
		self.assertEqual(transfer["on_hold"], False)
		self.assertNotIn("on_hold_until", transfer)

	def test_a_payment_without_splits_sends_no_transfers(self):
		make_split_payment(1000, [])

		self.assertNotIn("options", StubRazorpayClient.created_links[-1])

	def test_route_cannot_split_a_payment_in_another_currency(self):
		with self.assertRaisesRegex(frappe.ValidationError, "cannot split payments in"):
			make_split_payment(1000, [{"linked_account": self.account, "amount": 900}], currency="USD")

	def test_a_split_to_an_account_that_is_not_active_is_refused(self):
		pending = make_linked_account(owner="EUR", status="Pending")

		with self.assertRaisesRegex(frappe.ValidationError, "is not active"):
			make_split_payment(1000, [{"linked_account": pending, "amount": 900}])

	def test_splits_totalling_more_than_the_payment_are_refused(self):
		other = make_linked_account(owner="USD")

		with self.assertRaisesRegex(frappe.ValidationError, "more than the payment"):
			make_split_payment(
				1000,
				[
					{"linked_account": self.account, "amount": 600},
					{"linked_account": other, "amount": 400.01},
				],
			)

	def test_two_splits_to_one_account_are_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "only one split to each linked account"):
			make_split_payment(
				1000,
				[
					{"linked_account": self.account, "amount": 100},
					{"linked_account": self.account, "amount": 100},
				],
			)

	def test_paying_records_the_transfer_razorpay_made(self):
		payment_request = make_split_payment(
			1000, [{"linked_account": self.account, "amount": 900, "hold_until": self.hold_until}]
		)

		pay(payment_request)

		split = payment_request.splits[0]
		self.assertEqual(payment_request.status, "Paid")
		self.assertTrue(split.transfer_id.startswith("trf_"))
		self.assertEqual(split.transfer_status, "processed")
		self.assertEqual(split.settlement_status, "on_hold")

	def test_a_transfer_razorpay_cannot_report_yet_does_not_block_the_payment(self):
		frappe.db.set_single_value("Razorpay Gateway Settings", "treat_authorised_as_paid", 1)
		payment_request = make_split_payment(1000, [{"linked_account": self.account, "amount": 900}])
		StubRazorpayClient.add_payment(payment_request.order_ref, "authorized")

		with run_as_administrator():
			payment_request.sync_status()
		payment_request.reload()

		self.assertEqual(payment_request.status, "Paid")
		self.assertFalse(payment_request.splits[0].transfer_id)

	def test_the_daily_sync_reads_back_a_transfer_once_it_settles(self):
		payment_request = make_split_payment(1000, [{"linked_account": self.account, "amount": 900}])
		pay(payment_request)
		self.assertIn(payment_request.name, get_requests_with_open_splits())

		StubRazorpayClient.find_transfer(payment_request.splits[0].transfer_id).settlement_status = "settled"
		sync_open_split_transfers()

		payment_request.reload()
		self.assertEqual(payment_request.splits[0].settlement_status, "settled")
		self.assertNotIn(payment_request.name, get_requests_with_open_splits())

	def test_a_held_transfer_is_not_synced_before_its_hold_ends(self):
		payment_request = make_split_payment(
			1000, [{"linked_account": self.account, "amount": 900, "hold_until": self.hold_until}]
		)
		pay(payment_request)

		self.assertNotIn(payment_request.name, get_requests_with_open_splits())

	def test_moving_the_hold_moves_it_on_every_transfer(self):
		payment_request = make_split_payment(
			1000, [{"linked_account": self.account, "amount": 900, "hold_until": self.hold_until}]
		)
		pay(payment_request)
		later = add_days(self.hold_until, 5)

		api.set_split_hold(payment_request.name, later)

		self.assertEqual(
			StubRazorpayClient.updated_transfers,
			[
				{
					"transfer_id": payment_request.splits[0].transfer_id,
					"on_hold": True,
					"on_hold_until": int(to_aware_datetime(later).timestamp()),
				}
			],
		)
		payment_request.reload()
		self.assertEqual(get_datetime(payment_request.splits[0].hold_until), get_datetime(later))

	def test_moving_the_hold_reaches_a_transfer_not_seen_when_paid(self):
		payment_request = make_split_payment(
			1000, [{"linked_account": self.account, "amount": 900, "hold_until": self.hold_until}]
		)
		with patch.object(StubRazorpayClient, "get_payment_transfers", side_effect=frappe.ValidationError):
			pay(payment_request)
		self.assertFalse(payment_request.splits[0].transfer_id)

		api.set_split_hold(payment_request.name, add_days(self.hold_until, 5))

		self.assertEqual(len(StubRazorpayClient.updated_transfers), 1)

	def test_a_refund_without_splits_never_asks_for_transfers(self):
		payment_request = make_split_payment(1000, [])
		pay(payment_request)

		with patch.object(StubRazorpayClient, "get_payment_transfers", side_effect=frappe.ValidationError):
			api.refund(payment_request.name, 100)

		self.assertEqual(StubRazorpayClient.created_refunds[-1]["amount"], 10000)


class TestRazorpaySplitRefunds(RazorpaySplitTestCase):
	def setUp(self):
		super().setUp()
		self.payment_request = make_split_payment(1000, [{"linked_account": self.account, "amount": 900}])
		pay(self.payment_request)
		self.transfer_id = self.payment_request.splits[0].transfer_id

	def reversed_amounts(self) -> list[int]:
		return [reversal["amount"] for reversal in StubRazorpayClient.created_reversals]

	def test_a_full_refund_reverses_the_whole_transfer_first(self):
		api.refund(self.payment_request.name)

		self.assertEqual(
			StubRazorpayClient.created_reversals, [{"transfer_id": self.transfer_id, "amount": 90000}]
		)
		self.assertEqual(StubRazorpayClient.created_refunds[-1]["amount"], 100000)

	def test_partial_refunds_reverse_in_proportion_and_the_last_takes_what_is_left(self):
		api.refund(self.payment_request.name, 333.33)
		api.refund(self.payment_request.name, 666.67)

		self.assertEqual(self.reversed_amounts(), [29999, 60001])

	def test_a_refused_reversal_refunds_nothing(self):
		StubRazorpayClient.refuse_reversals = True

		with self.assertRaisesRegex(frappe.ValidationError, "balance is insufficient"):
			api.refund(self.payment_request.name, 100)

		self.assertEqual(StubRazorpayClient.created_refunds, [])

	def test_a_retry_after_a_failed_refund_does_not_reverse_twice(self):
		StubRazorpayClient.next_refund_status = "failed"
		with self.assertRaises(frappe.ValidationError):
			api.refund(self.payment_request.name, 500)

		StubRazorpayClient.next_refund_status = "processed"
		api.refund(self.payment_request.name, 500)

		self.assertEqual(self.reversed_amounts(), [45000])

	def test_a_failed_transfer_is_not_reversed(self):
		StubRazorpayClient.find_transfer(self.transfer_id).status = "failed"

		api.refund(self.payment_request.name, 500)

		self.assertEqual(StubRazorpayClient.created_reversals, [])
		self.assertEqual(StubRazorpayClient.created_refunds[-1]["amount"], 50000)
