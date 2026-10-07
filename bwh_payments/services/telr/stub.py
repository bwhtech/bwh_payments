from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.telr.base import (
	BaseTelrClient,
	TelrOrder,
	TelrRemoteResult,
	TelrStatus,
	TelrTransaction,
)


class StubTelrClient(BaseTelrClient):
	"""In-memory Telr, selected under tests. Drive outcomes through the classmethod controls; reset()
	between tests. Recordings keep Telr's own request shapes."""

	orders: ClassVar[dict[str, TelrOrder]] = {}
	created_orders: ClassVar[list[dict]] = []
	refunds: ClassVar[list[dict]] = []
	next_refund_status: ClassVar[str] = "A"

	@classmethod
	def reset(cls):
		cls.orders = {}
		cls.created_orders = []
		cls.refunds = []
		cls.next_refund_status = "A"

	# --- controls -----------------------------------------------------------------------------------

	@classmethod
	def add_order(cls, status_text: str = "Pending") -> str:
		"""An order that exists at Telr without this app having created it."""
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		order_ref = f"telr_{frappe.generate_hash(length=12)}"
		cls.orders[order_ref] = TelrOrder(
			ref=order_ref,
			url=f"https://secure.telr.test/gateway/process.html?o={order_ref}",
			status=TelrStatus(text=status_text),
		)
		return order_ref

	@classmethod
	def set_status(cls, order_ref: str, status_text: str):
		cls.orders[order_ref].status = TelrStatus(text=status_text)

	@classmethod
	def pay(cls, order_ref: str) -> str:
		"""The shopper completes the Hosted Payment Page: Telr settles a transaction against the order."""
		cls.set_status(order_ref, "Paid")
		transaction_ref = f"tran_{frappe.generate_hash(length=12)}"
		cls.orders[order_ref].transaction = TelrTransaction(ref=transaction_ref)
		return transaction_ref

	# --- API ----------------------------------------------------------------------------------------

	def create_order(self, order: dict, return_urls: dict, customer: dict) -> TelrOrder:
		self.created_orders.append({"order": order, "return": return_urls, "customer": customer})
		return self.check_order(self.add_order())

	def check_order(self, order_ref: str) -> TelrOrder:
		if order_ref not in self.orders:
			frappe.throw(_("Telr rejected the request: {0}").format("Order not found"))
		return self.orders[order_ref].model_copy(deep=True)

	def refund(
		self, transaction_ref: str, amount: str, currency: str, remote_auth_key: str
	) -> TelrRemoteResult:
		self.refunds.append({"ref": transaction_ref, "amount": amount, "currency": currency})
		if self.next_refund_status != "A":
			return TelrRemoteResult(status=self.next_refund_status, message="Refund declined")
		return TelrRemoteResult(status="A", message="Authorised", transaction_ref=f"rfnd_{transaction_ref}")

	def get_account_information(self) -> dict:
		return {}
