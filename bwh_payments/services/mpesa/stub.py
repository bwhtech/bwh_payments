import json
from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.mpesa.base import BaseMpesaClient, MpesaReversal, MpesaStkPush, MpesaStkQuery


class StubMpesaClient(BaseMpesaClient):
	"""In-memory Daraja for tests. Call reset() in setUp."""

	# CheckoutRequestID -> ResultCode; None while the shopper has not answered.
	pushes: ClassVar[dict[str, str | None]] = {}
	created_pushes: ClassVar[list[dict]] = []
	created_reversals: ClassVar[list[dict]] = []

	@classmethod
	def reset(cls):
		cls.pushes = {}
		cls.created_pushes = []
		cls.created_reversals = []

	@classmethod
	def add_push(cls, result_code: str | None = None) -> str:
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		checkout_request_id = f"ws_CO_{frappe.generate_hash(length=12)}"
		cls.pushes[checkout_request_id] = result_code
		return checkout_request_id

	@classmethod
	def set_result(cls, checkout_request_id: str, result_code: str | None):
		cls.pushes[checkout_request_id] = result_code

	@classmethod
	def pay(cls, checkout_request_id: str) -> str:
		"""The shopper enters their PIN. Returns the M-Pesa receipt number the callback carries."""
		cls.set_result(checkout_request_id, "0")
		return f"S{frappe.generate_hash(length=9).upper()}"

	def send_stk_push(self, payload: dict) -> MpesaStkPush:
		self.created_pushes.append(payload)
		return MpesaStkPush(checkout_request_id=self.add_push(), merchant_request_id="29115-1-1")

	def query_stk_push(self, checkout_request_id: str) -> MpesaStkQuery:
		if checkout_request_id not in self.pushes:
			frappe.throw(_("M-Pesa rejected the request: {0}").format("Invalid CheckoutRequestID"))
		return MpesaStkQuery(result_code=self.pushes[checkout_request_id])

	def reverse_transaction(self, payload: dict) -> MpesaReversal:
		self.created_reversals.append(payload)
		return MpesaReversal(conversation_id=f"AG_{frappe.generate_hash(length=16)}")


def build_stk_callback(
	checkout_request_id: str, result_code: int = 0, receipt: str | None = None, amount: int = 100
) -> bytes:
	callback = {
		"MerchantRequestID": "29115-1-1",
		"CheckoutRequestID": checkout_request_id,
		"ResultCode": result_code,
		"ResultDesc": "The service request is processed successfully.",
	}
	if result_code == 0:
		callback["CallbackMetadata"] = {
			"Item": [
				{"Name": "Amount", "Value": amount},
				{"Name": "MpesaReceiptNumber", "Value": receipt or "SAMPLE0001"},
				{"Name": "Balance"},
				{"Name": "TransactionDate", "Value": 20261019102115},
				{"Name": "PhoneNumber", "Value": 254708374149},
			]
		}
	return json.dumps({"Body": {"stkCallback": callback}}).encode()


def build_reversal_result(conversation_id: str, result_code: int = 0) -> bytes:
	return json.dumps(
		{
			"Result": {
				"ResultType": 0,
				"ResultCode": result_code,
				"ResultDesc": "The service request is processed successfully.",
				"OriginatorConversationID": "10571-7910404-1",
				"ConversationID": conversation_id,
				"TransactionID": "SREV000001",
			}
		}
	).encode()
