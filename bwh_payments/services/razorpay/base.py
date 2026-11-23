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


class RazorpayOrder(GatewayModel):
	id: str
	status: str | None = None
	# Minor units.
	amount: int | None = None
	currency: str | None = None


class RazorpayCard(GatewayModel):
	type: str | None = None


class RazorpayPaymentDetails(GatewayModel):
	"""A payment as GET /payments/{id} returns it."""

	id: str
	method: str | None = None
	card: RazorpayCard | None = None


class RazorpayRefund(GatewayModel):
	id: str | None = None
	status: str | None = None
	# Minor units, as Razorpay echoes it.
	amount: int | None = None


class BaseRazorpayClient(ABC):
	"""Razorpay's Payment Links, Orders and Refunds APIs.

	A rejected call raises `GatewayRefusedError`, and one Razorpay did not answer `GatewayUnavailableError`.
	"""

	@abstractmethod
	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink: ...

	@abstractmethod
	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def create_order(self, payload: dict) -> RazorpayOrder: ...

	@abstractmethod
	def get_order(self, order_id: str) -> RazorpayOrder: ...

	@abstractmethod
	def get_order_payments(self, order_id: str) -> list[RazorpayPayment]: ...

	@abstractmethod
	def get_payment(self, payment_id: str) -> RazorpayPaymentDetails: ...

	@abstractmethod
	def create_refund(
		self, payment_id: str, amount: int, idempotency_key: str | None = None
	) -> RazorpayRefund:
		"""Refund `amount` minor units of a captured payment. Resent with its key, Razorpay refunds once."""

	@abstractmethod
	def get_refund(self, refund_id: str) -> RazorpayRefund: ...
