from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel


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
	def create_checkout(self, payload: dict) -> TabbyCheckout: ...

	@abstractmethod
	def get_payment(self, payment_id: str) -> TabbyPayment: ...

	@abstractmethod
	def capture_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		"""`amount` is a major-unit decimal string."""

	@abstractmethod
	def refund_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		"""`amount` is a major-unit decimal string."""
