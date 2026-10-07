from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel

# Models mirror Telr's JSON, keeping only the fields this app reads.


class TelrStatus(GatewayModel):
	text: str | None = None


class TelrTransaction(GatewayModel):
	ref: str | None = None


class TelrOrder(GatewayModel):
	ref: str | None = None
	url: str | None = None
	status: TelrStatus | None = None
	transaction: TelrTransaction | None = None


class TelrRemoteResult(GatewayModel):
	"""The `<auth>` block of Telr's remote.xml reply. Status "A" means accepted."""

	status: str | None = None
	message: str | None = None
	transaction_ref: str | None = None


class BaseTelrClient(ABC):
	"""Telr's Hosted Payment Page and remote API. A rejected JSON call raises `frappe.ValidationError`."""

	@abstractmethod
	def create_order(self, order: dict, return_urls: dict, customer: dict) -> TelrOrder:
		"""`order.json` method `create`, with Telr's own `order`, `return` and `customer` blocks."""

	@abstractmethod
	def check_order(self, order_ref: str) -> TelrOrder: ...

	@abstractmethod
	def refund(
		self, transaction_ref: str, amount: str, currency: str, remote_auth_key: str
	) -> TelrRemoteResult:
		"""`remote.xml` refund of `amount`, a major-unit decimal string. Never raises on a refusal."""

	@abstractmethod
	def get_account_information(self) -> dict: ...
