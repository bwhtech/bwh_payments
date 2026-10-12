from abc import ABC, abstractmethod
from datetime import datetime

from bwh_payments.services import GatewayModel


class BraintreeTransaction(GatewayModel):
	id: str
	type: str | None = None
	status: str | None = None
	order_id: str | None = None
	# A major-unit decimal string; the SDK hands over a Decimal.
	amount: str | None = None
	currency_iso_code: str | None = None
	created_at: datetime | None = None


class BraintreeResult(GatewayModel):
	"""A sale, refund or void. A decline is `is_success=False`, not an exception."""

	is_success: bool = False
	message: str | None = None
	transaction: BraintreeTransaction | None = None


class BaseBraintreeClient(ABC):
	"""Braintree transactions through the SDK. Auth and network failures raise `frappe.ValidationError`."""

	@abstractmethod
	def generate_client_token(self, merchant_account_id: str) -> str:
		"""Raises when the keys are wrong or the merchant account does not exist."""

	@abstractmethod
	def sale(self, params: dict) -> BraintreeResult: ...

	@abstractmethod
	def find_transaction(self, transaction_id: str) -> BraintreeTransaction: ...

	@abstractmethod
	def search_sales(self, order_id: str) -> list[BraintreeTransaction]:
		"""Sales only: a refund is a credit transaction carrying the sale's `order_id`."""

	@abstractmethod
	def refund(self, transaction_id: str, amount: str) -> BraintreeResult: ...

	@abstractmethod
	def void(self, transaction_id: str) -> BraintreeResult: ...
