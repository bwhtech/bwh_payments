"""Razorpay Route: payment link transfers to linked accounts, their holds, and reversals on refund."""

from datetime import UTC, datetime

import frappe

from bwh_payments.base_class import PaymentSplit, SplitTransfer
from bwh_payments.currency import to_minor_units
from bwh_payments.services.razorpay.base import BaseRazorpayClient, RazorpayTransfer

ROUTE_CURRENCIES = ("INR",)
FAILED_TRANSFER_STATUS = "failed"


def build_transfer(split: PaymentSplit, currency: str) -> dict:
	"""One entry of a payment link's `options.order.transfers`."""
	return {
		"account": split.account_id,
		"amount": to_minor_units(split.amount, currency),
		"currency": currency.upper(),
		**build_hold(split.hold_until),
	}


def build_hold(hold_until: datetime | None) -> dict:
	if hold_until and hold_until > datetime.now(UTC):
		return {"on_hold": True, "on_hold_until": int(hold_until.timestamp())}
	return {"on_hold": False}


def read_split_transfer(transfer: RazorpayTransfer) -> SplitTransfer:
	return SplitTransfer(
		account_id=transfer.recipient,
		transfer_id=transfer.id,
		transfer_status=transfer.status,
		settlement_status=transfer.settlement_status,
	)


def has_splits(session_id: str) -> bool:
	request = frappe.db.get_value("Gateway Payment Request", {"order_ref": session_id})
	return bool(request and frappe.db.exists("Gateway Payment Split", {"parent": request}))


def reverse_split_transfers(client: BaseRazorpayClient, payment_id: str, refund_amount: int):
	"""Pull back each linked account's share of a refund before the refund leaves our balance.

	A refused reversal raises, so the shopper is never refunded with money we did not get back.
	"""
	transfers = [
		transfer
		for transfer in client.get_payment_transfers(payment_id)
		if transfer.status != FAILED_TRANSFER_STATUS
	]
	if not transfers:
		return

	payment = client.get_payment(payment_id)
	refunded = payment.amount_refunded + refund_amount
	for transfer in transfers:
		if (due := get_reversal_due(transfer, refunded, payment.amount)) > 0:
			client.reverse_transfer(transfer.id, due)


def get_reversal_due(transfer: RazorpayTransfer, refunded: int, paid: int) -> int:
	"""What to reverse so the account has given back its share of everything refunded so far.

	Cumulative, so a retry after a failed refund reverses nothing twice and the last refund takes
	exactly what is left. Minor units.
	"""
	target = transfer.amount if refunded >= paid else transfer.amount * refunded // paid
	return target - transfer.amount_reversed
