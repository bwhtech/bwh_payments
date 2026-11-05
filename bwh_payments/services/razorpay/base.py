from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel


class RazorpayPayment(GatewayModel):
	payment_id: str
	status: str | None = None


class RazorpayPaymentLink(GatewayModel):
	id: str
	status: str | None = None
	short_url: str | None = None
	payments: list[RazorpayPayment] | None = None


class RazorpayRefund(GatewayModel):
	id: str | None = None
	status: str | None = None
	# Minor units, as Razorpay echoes it.
	amount: int | None = None


class BaseRazorpayClient(ABC):
	"""Razorpay's Payment Links and Refunds APIs. A rejected call raises `frappe.ValidationError`."""

	@abstractmethod
	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink: ...

	@abstractmethod
	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def create_refund(self, payment_id: str, amount: int) -> RazorpayRefund:
		"""Refund `amount` minor units of a captured payment."""

	@abstractmethod
	def get_refund(self, refund_id: str) -> RazorpayRefund: ...
