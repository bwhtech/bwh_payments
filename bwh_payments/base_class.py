from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict


class GatewayResult(BaseModel):
	# Rejecting a numeric refund id after money moved would leave the refund unrecorded.
	model_config = ConfigDict(coerce_numbers_to_str=True)


class CheckoutSession(GatewayResult):
	session_id: str
	redirect_url: str
	success_url: str | None = None
	cancel_url: str | None = None
	failure_url: str | None = None


class RefundResult(GatewayResult):
	# All optional: read after money moved, so a thin result must not fail.
	refund_id: str | None = None
	status: str | None = None
	amount: float | None = None


class WebhookEvent(GatewayResult):
	session_id: str
	status: str
	event_id: str | None = None


class PaymentGatewayBase(ABC):
	"""Contract for every `<Gateway> Gateway Settings` Single. Amounts are in major units (12.34)."""

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
		"""Return a Gateway Payment Request `status` value."""

	@abstractmethod
	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult: ...

	@abstractmethod
	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		"""Verify the signature, then return the event, or None to ignore the delivery."""

	def cancel_session(self, session_id: str) -> bool:
		return False

	def get_gateway_name(self) -> str:
		return self.__class__.__name__
