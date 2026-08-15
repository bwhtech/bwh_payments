from abc import ABC, abstractmethod


class PaymentGatewayBase(ABC):
	"""Contract every `<Gateway> Gateway Settings` Single must satisfy to back a Payment Gateway Profile.

	All amounts crossing this boundary are in MAJOR units (12.34, not 1234); each gateway converts to its
	own minor units with `bwh_payments.currency.to_minor_units` so charge and refund always agree.
	"""

	@abstractmethod
	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> dict:
		"""Return {"session_id", "redirect_url", "success_url", "cancel_url", "failure_url"}."""

	@abstractmethod
	def get_payment_status(self, session_id: str) -> str:
		"""Return one of the Gateway Payment Request `status` values, read from the gateway."""

	@abstractmethod
	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> dict:
		"""Return {"refund_id", "status", "amount"} with amount in major units."""

	@abstractmethod
	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		"""Verify the signature, then return {} to ignore or {"session_id", "status", "event_id"}."""

	def get_gateway_name(self) -> str:
		return self.__class__.__name__
