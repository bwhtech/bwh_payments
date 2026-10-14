import hashlib
import json
from typing import ClassVar
from urllib.parse import urlencode

import frappe
from frappe import _

from bwh_payments.services.payu.base import BasePayUClient, PayURefund, PayUTransaction


class StubPayUClient(BasePayUClient):
	"""In-memory PayU for tests. Call reset() in setUp."""

	transactions: ClassVar[dict[str, PayUTransaction]] = {}
	refunds: ClassVar[dict[str, PayURefund]] = {}
	# PayU's own shape: var1 = mihpayid, var2 = token, var3 = amount.
	created_refunds: ClassVar[list[dict]] = []
	refund_error: ClassVar[str | None] = None

	@classmethod
	def reset(cls):
		cls.transactions = {}
		cls.refunds = {}
		cls.created_refunds = []
		cls.refund_error = None

	@classmethod
	def set_status(cls, txnid: str, unmappedstatus: str) -> str:
		transaction = cls.transactions.setdefault(
			txnid, PayUTransaction(mihpayid=frappe.generate_hash(length=12))
		)
		transaction.unmappedstatus = unmappedstatus
		transaction.status = "success" if unmappedstatus == "captured" else unmappedstatus
		return transaction.mihpayid

	@classmethod
	def pay(cls, txnid: str) -> str:
		"""Capture the payment and return its mihpayid."""
		return cls.set_status(txnid, "captured")

	@classmethod
	def set_refund_status(cls, request_id: str, status: str):
		cls.refunds[request_id].status = status

	@classmethod
	def refuse_next_refund(cls, message: str = "Refund amount exceeds the captured amount"):
		cls.refund_error = message

	def verify_payment(self, txnid: str) -> PayUTransaction:
		if txnid not in self.transactions:
			return PayUTransaction(mihpayid="Not Found", status="Not Found")
		return self.transactions[txnid].model_copy()

	def refund(self, mihpayid: str, token: str, amount: str) -> PayURefund:
		self.created_refunds.append({"var1": mihpayid, "var2": token, "var3": amount})
		if message := self.refund_error:
			type(self).refund_error = None
			frappe.throw(_("PayU rejected the request: {0}").format(message))
		if not any(transaction.mihpayid == mihpayid for transaction in self.transactions.values()):
			frappe.throw(_("PayU rejected the request: {0}").format("invalid mihpayid"))

		refund = PayURefund(
			request_id=frappe.generate_hash(length=10), status="QUEUED", amt=amount, mihpayid=mihpayid
		)
		self.refunds[refund.request_id] = refund
		return refund.model_copy()

	def get_refund(self, request_id: str) -> PayURefund:
		if request_id not in self.refunds:
			frappe.throw(_("PayU rejected the request: {0}").format("invalid request id"))
		return self.refunds[request_id].model_copy()


def build_payment_event(
	txnid: str,
	key: str,
	salt: str,
	unmappedstatus: str = "captured",
	additional_charges: str | None = None,
	amount: str = "100.00",
) -> bytes:
	"""The form PayU posts to surl, furl and the payment webhook, signed with the reverse hash."""
	transaction = StubPayUClient.transactions.get(txnid)
	fields = {
		"mihpayid": transaction.mihpayid if transaction else frappe.generate_hash(length=12),
		"key": key,
		"txnid": txnid,
		"amount": amount,
		"productinfo": "GPR-TEST",
		"firstname": "Asha",
		"email": "asha@example.com",
		"phone": "9876543210",
		"status": "success" if unmappedstatus == "captured" else "failure",
		"unmappedstatus": unmappedstatus,
		**{f"udf{index}": "" for index in range(1, 6)},
	}
	# Written out apart from the controller, so a wrong formula there fails the tests.
	reverse = (
		f"{salt}|{fields['status']}|||||||||||{fields['email']}|{fields['firstname']}"
		f"|{fields['productinfo']}|{amount}|{txnid}|{key}"
	)
	if additional_charges:
		fields["additionalCharges"] = additional_charges
		reverse = f"{additional_charges}|{reverse}"
	fields["hash"] = hashlib.sha512(reverse.encode()).hexdigest()
	return urlencode(fields).encode()


def build_refund_event(request_id: str, **fields) -> bytes:
	"""PayU's unsigned refund webhook. `fields` overrides what the stub knows, to forge a body."""
	refund = StubPayUClient.refunds.get(request_id) or PayURefund(request_id=request_id)
	return json.dumps({**refund.model_dump(), "action": "refund", **fields}).encode()
