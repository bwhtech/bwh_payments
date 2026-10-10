from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel

# Models mirror Tabby's JSON, keeping only the fields this app reads. Tabby leaves blocks out or sends
# null freely, so every field is optional and the controller decides what a missing one means.


class TabbyRefund(GatewayModel):
	id: str | None = None


class TabbyPayment(GatewayModel):
	id: str | None = None
	status: str | None = None
	# A major-unit decimal string, though Tabby has been seen to send a bare number.
	amount: str | float | None = None
	currency: str | None = None
	refunds: list[TabbyRefund] | None = None


class TabbyInstallmentPlan(GatewayModel):
	web_url: str | None = None


class TabbyAvailableProducts(GatewayModel):
	installments: list[TabbyInstallmentPlan] | None = None


class TabbyProductStatus(GatewayModel):
	rejection_reason: str | None = None


class TabbyProducts(GatewayModel):
	installments: TabbyProductStatus | None = None


class TabbyConfiguration(GatewayModel):
	available_products: TabbyAvailableProducts | None = None
	products: TabbyProducts | None = None


class TabbyCheckout(GatewayModel):
	id: str | None = None
	status: str | None = None
	payment: TabbyPayment | None = None
	configuration: TabbyConfiguration | None = None


class BaseTabbyClient(ABC):
	"""Tabby's Checkout and Payments APIs. A rejected call raises `frappe.ValidationError`."""

	@abstractmethod
	def create_checkout(self, payload: dict) -> TabbyCheckout:
		"""POST /api/v2/checkout with the body exactly as Tabby documents it."""

	@abstractmethod
	def get_payment(self, payment_id: str) -> TabbyPayment: ...

	@abstractmethod
	def capture_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		"""Capture `amount`, a major-unit decimal string, and return the payment Tabby answers with."""

	@abstractmethod
	def refund_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		"""Refund `amount`, a major-unit decimal string. Tabby answers with the updated payment."""
