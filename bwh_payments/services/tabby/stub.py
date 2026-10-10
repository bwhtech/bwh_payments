from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.tabby.base import (
	BaseTabbyClient,
	TabbyAvailableProducts,
	TabbyCheckout,
	TabbyConfiguration,
	TabbyInstallmentPlan,
	TabbyPayment,
	TabbyProducts,
	TabbyProductStatus,
	TabbyRefund,
)


class StubTabbyClient(BaseTabbyClient):
	"""In-memory Tabby, selected under tests. Drive outcomes through the classmethod controls; reset()
	between tests. Recordings keep Tabby's own request shapes."""

	payments: ClassVar[dict[str, TabbyPayment]] = {}
	created_checkouts: ClassVar[list[dict]] = []
	captures: ClassVar[list[dict]] = []
	refunds: ClassVar[list[dict]] = []
	next_rejection_reason: ClassVar[str | None] = None

	@classmethod
	def reset(cls):
		cls.payments = {}
		cls.created_checkouts = []
		cls.captures = []
		cls.refunds = []
		cls.next_rejection_reason = None

	# --- controls -----------------------------------------------------------------------------------

	@classmethod
	def add_payment(cls, status: str = "created", amount: str = "100.00", currency: str = "SAR") -> str:
		"""A payment that exists at Tabby without this app having created it."""
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		payment_id = f"pay_tabby_{frappe.generate_hash(length=12)}"
		cls.payments[payment_id] = TabbyPayment(
			id=payment_id, status=status, amount=amount, currency=currency, refunds=[]
		)
		return payment_id

	@classmethod
	def set_status(cls, payment_id: str, status: str):
		cls.payments[payment_id].status = status

	@classmethod
	def authorise(cls, payment_id: str):
		"""The shopper is approved for instalments; no money moves until this app captures."""
		cls.set_status(payment_id, "authorized")

	@classmethod
	def reject_next_checkout(cls, reason: str):
		"""Tabby declines the next shopper, e.g. "order_amount_too_high"."""
		cls.next_rejection_reason = reason

	# --- API ----------------------------------------------------------------------------------------

	def create_checkout(self, payload: dict) -> TabbyCheckout:
		self.created_checkouts.append(payload)
		if reason := self.next_rejection_reason:
			StubTabbyClient.next_rejection_reason = None
			return TabbyCheckout(
				status="rejected",
				configuration=TabbyConfiguration(
					available_products=TabbyAvailableProducts(installments=[]),
					products=TabbyProducts(installments=TabbyProductStatus(rejection_reason=reason)),
				),
			)

		payment_id = self.add_payment(
			amount=payload["payment"]["amount"], currency=payload["payment"]["currency"]
		)
		return TabbyCheckout(
			id=f"chk_{payment_id}",
			status="created",
			payment=self.get_payment(payment_id),
			configuration=TabbyConfiguration(
				available_products=TabbyAvailableProducts(
					installments=[TabbyInstallmentPlan(web_url=f"https://checkout.tabby.test/{payment_id}")]
				)
			),
		)

	def get_payment(self, payment_id: str) -> TabbyPayment:
		if payment_id not in self.payments:
			frappe.throw(_("Tabby rejected the request: {0}").format("payment not found"))
		return self.payments[payment_id].model_copy(deep=True)

	def capture_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		self.get_payment(payment_id)
		self.captures.append({"payment_id": payment_id, "amount": amount})
		self.set_status(payment_id, "closed")
		return self.get_payment(payment_id)

	def refund_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		self.get_payment(payment_id)
		self.refunds.append({"payment_id": payment_id, "amount": amount})
		self.payments[payment_id].refunds.append(TabbyRefund(id=f"rfnd_{frappe.generate_hash(length=12)}"))
		return self.get_payment(payment_id)
