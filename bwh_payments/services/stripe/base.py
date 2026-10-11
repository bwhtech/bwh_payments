from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel


class StripeCheckoutSession(GatewayModel):
	id: str
	url: str | None = None
	status: str | None = None
	payment_status: str | None = None
	payment_intent: str | None = None
	currency: str | None = None
	success_url: str | None = None
	cancel_url: str | None = None


class StripeRefund(GatewayModel):
	id: str | None = None
	status: str | None = None
	# Minor units, as Stripe echoes it.
	amount: int | None = None


class BaseStripeClient(ABC):
	"""Stripe Checkout Sessions and Refunds. A refusal raises the SDK's own `stripe.StripeError`."""

	@abstractmethod
	def create_checkout_session(self, params: dict) -> StripeCheckoutSession: ...

	@abstractmethod
	def retrieve_checkout_session(self, session_id: str) -> StripeCheckoutSession: ...

	@abstractmethod
	def expire_checkout_session(self, session_id: str) -> StripeCheckoutSession: ...

	@abstractmethod
	def create_refund(self, params: dict) -> StripeRefund: ...

	@abstractmethod
	def retrieve_refund(self, refund_id: str) -> StripeRefund: ...
