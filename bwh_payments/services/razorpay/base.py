from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

# Models mirror Razorpay's JSON, keeping only the fields this app reads, so a live response parses as-is.


class RazorpayPayment(BaseModel):
	model_config = ConfigDict(extra="ignore")

	payment_id: str
	status: str | None = None


class RazorpayPaymentLink(BaseModel):
	model_config = ConfigDict(extra="ignore")

	id: str
	status: str | None = None
	short_url: str | None = None
	# Razorpay sends null rather than [] for a link nobody has paid against.
	payments: list[RazorpayPayment] | None = None


class RazorpayRefund(BaseModel):
	model_config = ConfigDict(extra="ignore")

	id: str
	status: str | None = None
	# Minor units, as Razorpay echoes it.
	amount: int


class BaseRazorpayClient(ABC):
	"""Razorpay's Payment Links and Refunds APIs. A rejected call raises `frappe.ValidationError`."""

	@abstractmethod
	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink:
		"""POST /payment_links with the body exactly as Razorpay documents it."""

	@abstractmethod
	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def create_refund(self, payment_id: str, amount: int) -> RazorpayRefund:
		"""Refund `amount` minor units of a captured payment."""
