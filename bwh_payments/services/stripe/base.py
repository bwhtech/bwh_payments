from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

# Models mirror Stripe's objects, keeping only the fields this app reads.


class StripeCheckoutSession(BaseModel):
	model_config = ConfigDict(extra="ignore")

	id: str
	url: str | None = None
	status: str | None = None
	payment_status: str | None = None
	payment_intent: str | None = None
	currency: str | None = None
	success_url: str | None = None
	cancel_url: str | None = None


class StripeRefund(BaseModel):
	model_config = ConfigDict(extra="ignore")

	id: str
	status: str | None = None
	# Minor units, as Stripe echoes it.
	amount: int


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
