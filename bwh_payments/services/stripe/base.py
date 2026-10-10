from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel

# Models mirror Stripe's objects, keeping only the fields this app reads.


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
	# Parsed after the money has moved, so nothing here may be required: a thin echo must not strand
	# the refund unrecorded and refundable again.
	id: str | None = None
	status: str | None = None
	# Minor units, as Stripe echoes it.
	amount: int | None = None


class BaseStripeClient(ABC):
	"""Stripe Checkout Sessions and Refunds. A refusal raises the SDK's own `stripe.StripeError`."""

	@abstractmethod
	def create_checkout_session(self, params: dict) -> StripeCheckoutSession:
		"""`checkout.sessions.create` with params exactly as Stripe documents them."""

	@abstractmethod
	def retrieve_checkout_session(self, session_id: str) -> StripeCheckoutSession: ...

	@abstractmethod
	def expire_checkout_session(self, session_id: str) -> StripeCheckoutSession: ...

	@abstractmethod
	def create_refund(self, params: dict) -> StripeRefund: ...
