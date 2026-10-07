from abc import ABC, abstractmethod

from pydantic import BaseModel


class CheckoutSession(BaseModel):
	session_id: str
	redirect_url: str
	success_url: str | None = None
	cancel_url: str | None = None
	failure_url: str | None = None


class RefundResult(BaseModel):
	refund_id: str | None = None
	status: str
	# Major units, round-tripped from the gateway's own echo where it gives one.
	amount: float


class WebhookEvent(BaseModel):
	session_id: str
	# A Gateway Payment Request `status` value.
	status: str
	event_id: str | None = None


class PaymentGatewayBase(ABC):
	"""Contract every `<Gateway> Gateway Settings` Single must satisfy to back a Payment Gateway Profile.

	All amounts crossing this boundary are in MAJOR units (12.34, not 1234). What each gateway does with
	them is its own business: Stripe converts to ISO minor units with `bwh_payments.currency.to_minor_units`,
	while Telr bills in major units and only uses the currency's minor-unit exponent to decide how many
	decimals to send. Either way charge and refund go through the same conversion, so they always agree.

	Callers read every result through `<Model>.model_validate`, so a gateway from another app that still
	returns plain dicts keeps working, and one that returns garbage fails loudly at the boundary.
	"""

	@abstractmethod
	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> CheckoutSession: ...

	@abstractmethod
	def get_payment_status(self, session_id: str) -> str:
		"""Return one of the Gateway Payment Request `status` values, read from the gateway."""

	@abstractmethod
	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult: ...

	@abstractmethod
	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		"""Verify the signature, then return None to ignore the delivery or the event it carries."""

	def cancel_session(self, session_id: str) -> bool:
		return False

	def get_gateway_name(self) -> str:
		return self.__class__.__name__
