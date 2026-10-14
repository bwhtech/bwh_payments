from abc import ABC, abstractmethod

import frappe

from bwh_payments.services import GatewayModel


class PayPalOrderAlreadyCapturedError(frappe.ValidationError):
	"""PayPal's `ORDER_ALREADY_CAPTURED`: another call took the money first."""


class PayPalLink(GatewayModel):
	rel: str | None = None
	href: str | None = None


class PayPalCapture(GatewayModel):
	id: str | None = None
	status: str | None = None


class PayPalPayments(GatewayModel):
	captures: list[PayPalCapture] | None = None


class PayPalPurchaseUnit(GatewayModel):
	payments: PayPalPayments | None = None


class PayPalOrder(GatewayModel):
	id: str
	status: str | None = None
	links: list[PayPalLink] | None = None
	purchase_units: list[PayPalPurchaseUnit] | None = None


class PayPalAmount(GatewayModel):
	currency_code: str | None = None
	# A major-unit decimal string.
	value: str | None = None


class PayPalRefund(GatewayModel):
	id: str | None = None
	status: str | None = None
	amount: PayPalAmount | None = None
	links: list[PayPalLink] | None = None


class PayPalWebhookVerification(GatewayModel):
	verification_status: str | None = None


class BasePayPalClient(ABC):
	"""PayPal Orders v2, Payments v2 and webhook verification. A refusal raises `frappe.ValidationError`."""

	@abstractmethod
	def create_order(self, payload: dict, request_id: str | None = None) -> PayPalOrder: ...

	@abstractmethod
	def get_order(self, order_id: str) -> PayPalOrder: ...

	@abstractmethod
	def capture_order(self, order_id: str) -> PayPalOrder:
		"""Raises `PayPalOrderAlreadyCapturedError` when the order was already captured."""

	@abstractmethod
	def refund_capture(self, capture_id: str, payload: dict) -> PayPalRefund: ...

	@abstractmethod
	def get_refund(self, refund_id: str) -> PayPalRefund: ...

	@abstractmethod
	def verify_webhook_signature(self, payload: dict) -> PayPalWebhookVerification: ...
