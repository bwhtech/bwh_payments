import json
from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.paypal.base import (
	BasePayPalClient,
	PayPalAmount,
	PayPalCapture,
	PayPalLink,
	PayPalOrder,
	PayPalOrderAlreadyCapturedError,
	PayPalPayments,
	PayPalPurchaseUnit,
	PayPalRefund,
	PayPalWebhookVerification,
)

# The five headers PayPal signs a delivery with, in the title case werkzeug hands over.
SIGNATURE_HEADERS = {
	"Paypal-Auth-Algo": "SHA256withRSA",
	"Paypal-Cert-Url": "https://api.sandbox.paypal.com/cert.pem",
	"Paypal-Transmission-Id": "transmission-test-0001",
	"Paypal-Transmission-Sig": "signature-test-0001",
	"Paypal-Transmission-Time": "2026-09-03T00:00:00Z",
}


class StubPayPalClient(BasePayPalClient):
	"""In-memory PayPal for tests. Call reset() in setUp."""

	orders: ClassVar[dict[str, PayPalOrder]] = {}
	created_orders: ClassVar[list[dict]] = []
	captured_orders: ClassVar[list[str]] = []
	refunds: ClassVar[list[dict]] = []
	issued_refunds: ClassVar[dict[str, PayPalRefund]] = {}
	verified_events: ClassVar[list[dict]] = []
	next_refund_status: ClassVar[str] = "COMPLETED"
	next_verification_status: ClassVar[str] = "SUCCESS"
	# An issue code such as "INSTRUMENT_DECLINED" makes the next capture fail with it.
	next_capture_issue: ClassVar[str | None] = None
	# Another call (the return page or the webhook) captures between our read and our capture.
	lose_next_capture_race: ClassVar[bool] = False

	@classmethod
	def reset(cls):
		cls.orders = {}
		cls.created_orders = []
		cls.captured_orders = []
		cls.refunds = []
		cls.issued_refunds = {}
		cls.verified_events = []
		cls.next_refund_status = "COMPLETED"
		cls.next_verification_status = "SUCCESS"
		cls.next_capture_issue = None
		cls.lose_next_capture_race = False

	@classmethod
	def add_order(cls, status: str = "CREATED") -> str:
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		order_id = f"PAYPAL{frappe.generate_hash(length=12).upper()}"
		cls.orders[order_id] = PayPalOrder(
			id=order_id,
			status=status,
			links=[
				PayPalLink(rel="self", href=f"https://api.paypal.test/v2/checkout/orders/{order_id}"),
				PayPalLink(rel="payer-action", href=f"https://paypal.test/checkoutnow?token={order_id}"),
			],
			purchase_units=[PayPalPurchaseUnit(payments=PayPalPayments(captures=[]))],
		)
		return order_id

	@classmethod
	def set_status(cls, order_id: str, status: str):
		cls.orders[order_id].status = status

	@classmethod
	def add_capture(cls, order_id: str, status: str = "COMPLETED") -> str:
		capture_id = f"CAPTURE{frappe.generate_hash(length=12).upper()}"
		cls.orders[order_id].purchase_units[0].payments.captures.append(
			PayPalCapture(id=capture_id, status=status)
		)
		return capture_id

	@classmethod
	def set_refund_status(cls, refund_id: str, status: str):
		"""PayPal settles a PENDING refund later, as COMPLETED or FAILED."""
		cls.issued_refunds[refund_id].status = status

	@classmethod
	def approve(cls, order_id: str):
		"""The shopper approves on PayPal; no money moves until a capture."""
		cls.set_status(order_id, "APPROVED")

	@classmethod
	def pay(cls, order_id: str) -> str:
		"""Approve and capture, as when another call won the capture race."""
		cls.set_status(order_id, "COMPLETED")
		return cls.add_capture(order_id)

	def create_order(self, payload: dict, request_id: str | None = None) -> PayPalOrder:
		self.created_orders.append(payload)
		return self.get_order(self.add_order())

	def get_order(self, order_id: str) -> PayPalOrder:
		if order_id not in self.orders:
			frappe.throw(
				_("PayPal rejected the request: {0}").format("The specified resource does not exist.")
			)
		return self.orders[order_id].model_copy(deep=True)

	def capture_order(self, order_id: str) -> PayPalOrder:
		if self.lose_next_capture_race:
			StubPayPalClient.lose_next_capture_race = False
			self.pay(order_id)

		order = self.get_order(order_id)
		if order.status == "COMPLETED":
			frappe.throw(
				_("PayPal rejected the request: {0}").format("Order already captured."),
				exc=PayPalOrderAlreadyCapturedError,
			)
		if issue := self.next_capture_issue:
			frappe.throw(_("PayPal rejected the request: {0}").format(issue))

		self.captured_orders.append(order_id)
		self.pay(order_id)
		return self.get_order(order_id)

	def refund_capture(self, capture_id: str, payload: dict) -> PayPalRefund:
		self.refunds.append({"capture_id": capture_id, **payload})
		amount = payload.get("amount") or {}
		refund = build_refund(
			capture_id, self.next_refund_status, amount.get("value"), amount.get("currency_code")
		)
		self.issued_refunds[refund.id] = refund
		return refund.model_copy(deep=True)

	def get_refund(self, refund_id: str) -> PayPalRefund:
		if refund_id not in self.issued_refunds:
			frappe.throw(
				_("PayPal rejected the request: {0}").format("The specified resource does not exist.")
			)
		return self.issued_refunds[refund_id].model_copy(deep=True)

	def verify_webhook_signature(self, payload: dict) -> PayPalWebhookVerification:
		self.verified_events.append(payload)
		return PayPalWebhookVerification(verification_status=self.next_verification_status)


def build_order_approved_event(order_id: str, event_id: str = "WH-TEST-APPROVED-0001") -> bytes:
	return json.dumps(
		{
			"id": event_id,
			"event_type": "CHECKOUT.ORDER.APPROVED",
			"resource": {"id": order_id, "status": "APPROVED"},
		}
	).encode()


def build_capture_completed_event(order_id: str, event_id: str = "WH-TEST-CAPTURE-0001") -> bytes:
	"""`resource.id` is the capture; the order id is only in supplementary data."""
	return json.dumps(
		{
			"id": event_id,
			"event_type": "PAYMENT.CAPTURE.COMPLETED",
			"resource": {
				"id": f"CAPTURE{frappe.generate_hash(length=8).upper()}",
				"status": "COMPLETED",
				"supplementary_data": {"related_ids": {"order_id": order_id}},
			},
		}
	).encode()


def build_refund_event(
	refund: PayPalRefund,
	event_type: str = "PAYMENT.CAPTURE.REFUNDED",
	event_id: str | None = None,
) -> bytes:
	return json.dumps(
		{
			"id": event_id or f"WH-TEST-REFUND-{frappe.generate_hash(length=8).upper()}",
			"event_type": event_type,
			"resource": refund.model_dump(exclude_none=True),
		}
	).encode()


def build_refund(
	capture_id: str, status: str = "COMPLETED", value: str = "5.00", currency_code: str = "USD"
) -> PayPalRefund:
	"""A refund as PayPal sends it, naming its capture only in the `up` link."""
	refund_id = f"REFUND{frappe.generate_hash(length=12).upper()}"
	return PayPalRefund(
		id=refund_id,
		status=status,
		amount=PayPalAmount(currency_code=currency_code, value=value),
		links=[
			PayPalLink(rel="self", href=f"https://api.paypal.test/v2/payments/refunds/{refund_id}"),
			PayPalLink(rel="up", href=f"https://api.paypal.test/v2/payments/captures/{capture_id}"),
		],
	)
