from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, model_validator


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
	"""A payment event carries `session_id`; a refund event carries `refund_id` instead."""

	session_id: str | None = None
	status: str
	event_id: str | None = None
	refund_id: str | None = None
	# Major units. Needed to record a refund raised outside this app.
	refund_amount: float | None = None
	# The settled payment's id, which finds the request for a refund raised outside this app.
	transaction_ref: str | None = None

	@model_validator(mode="after")
	def require_session_or_refund(self):
		if not (self.session_id or self.refund_id):
			raise ValueError("A webhook event needs a session_id or a refund_id")
		return self


class PaymentGatewayBase(ABC):
	"""Contract for every `<Gateway> Gateway Settings` Single. Amounts are in major units (12.34)."""

	@abstractmethod
	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
	) -> CheckoutSession:
		"""With `return_url`, every way back from the checkout (paid, failed, cancelled) goes there."""

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

	def get_refund_status(self, session_id: str, refund_id: str) -> RefundResult | None:
		"""What the gateway now says about a refund, or None when it cannot be asked."""
		return None

	def get_transaction_ref(self, session_id: str) -> str | None:
		"""The gateway's id for the settled payment, or None when it cannot say."""
		return None

	def get_gateway_name(self) -> str:
		return self.__class__.__name__
