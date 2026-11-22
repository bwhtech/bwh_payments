import hashlib
import hmac
import json
from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services import GatewayUnavailableError
from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayOrder,
	RazorpayPayment,
	RazorpayPaymentDetails,
	RazorpayPaymentLink,
	RazorpayRefund,
)


class StubRazorpayClient(BaseRazorpayClient):
	"""In-memory Razorpay for tests. Call reset() in setUp."""

	links: ClassVar[dict[str, RazorpayPaymentLink]] = {}
	orders: ClassVar[dict[str, RazorpayOrder]] = {}
	order_payments: ClassVar[dict[str, list[RazorpayPayment]]] = {}
	# GET /payments/{id} responses, by payment id.
	payments: ClassVar[dict[str, dict]] = {}
	created_links: ClassVar[list[dict]] = []
	created_orders: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	refunds: ClassVar[dict[str, RazorpayRefund]] = {}
	refund_keys: ClassVar[dict[str, str]] = {}
	next_refund_status: ClassVar[str] = "processed"
	# Razorpay makes the next refund, but its answer never arrives.
	lose_next_refund_answer: ClassVar[bool] = False

	@classmethod
	def reset(cls):
		cls.links = {}
		cls.orders = {}
		cls.order_payments = {}
		cls.payments = {}
		cls.created_links = []
		cls.created_orders = []
		cls.created_refunds = []
		cls.refunds = {}
		cls.refund_keys = {}
		cls.next_refund_status = "processed"
		cls.lose_next_refund_answer = False

	@classmethod
	def set_refund_status(cls, refund_id: str, status: str):
		cls.refunds[refund_id].status = status

	@classmethod
	def add_link(cls, status: str = "created") -> str:
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		link_id = f"plink_{frappe.generate_hash(length=12)}"
		cls.links[link_id] = RazorpayPaymentLink(
			id=link_id, status=status, short_url=f"https://rzp.io/i/{link_id}", payments=[]
		)
		return link_id

	@classmethod
	def add_order(cls, amount: int, currency: str) -> str:
		order_id = f"order_{frappe.generate_hash(length=12)}"
		cls.orders[order_id] = RazorpayOrder(id=order_id, status="created", amount=amount, currency=currency)
		cls.order_payments[order_id] = []
		return order_id

	@classmethod
	def set_status(cls, session_id: str, status: str):
		"""`session_id` is a payment link or an order."""
		(cls.orders.get(session_id) or cls.links[session_id]).status = status

	@classmethod
	def add_payment(cls, session_id: str, status: str, response: dict | None = None) -> str:
		"""`response` is what GET /payments/{id} answers, for example one recorded from Razorpay."""
		payment_id = f"pay_{frappe.generate_hash(length=12)}"
		payments = cls.order_payments.get(session_id)
		if payments is None:
			payments = cls.links[session_id].payments
		payments.append(RazorpayPayment(payment_id=payment_id, status=status))
		cls.payments[payment_id] = {"method": "upi", **(response or {}), "id": payment_id, "status": status}
		return payment_id

	@classmethod
	def pay(cls, session_id: str, response: dict | None = None) -> str:
		"""Capture a payment and close the link or order."""
		cls.set_status(session_id, "paid")
		return cls.add_payment(session_id, "captured", response)

	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink:
		self.created_links.append(payload)
		return self.get_payment_link(self.add_link())

	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		if link_id not in self.links:
			frappe.throw(_("Razorpay rejected the request: {0}").format("payment link not found"))
		return self.links[link_id].model_copy(deep=True)

	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		link = self.get_payment_link(link_id)
		if link.status != "created":
			frappe.throw(
				_("Razorpay rejected the request: {0}").format(
					f"payment link is not cancellable in state {link.status}"
				)
			)
		self.set_status(link_id, "cancelled")
		return self.get_payment_link(link_id)

	def create_order(self, payload: dict) -> RazorpayOrder:
		self.created_orders.append(payload)
		return self.get_order(self.add_order(payload["amount"], payload["currency"]))

	def get_order(self, order_id: str) -> RazorpayOrder:
		if order_id not in self.orders:
			frappe.throw(_("Razorpay rejected the request: {0}").format("order not found"))
		return self.orders[order_id].model_copy()

	def get_order_payments(self, order_id: str) -> list[RazorpayPayment]:
		self.get_order(order_id)
		return [payment.model_copy() for payment in self.order_payments[order_id]]

	def get_payment(self, payment_id: str) -> RazorpayPaymentDetails:
		if payment_id not in self.payments:
			frappe.throw(_("Razorpay rejected the request: {0}").format("payment not found"))
		return RazorpayPaymentDetails.model_validate(self.payments[payment_id])

	def create_refund(
		self, payment_id: str, amount: int, idempotency_key: str | None = None
	) -> RazorpayRefund:
		"""A key Razorpay has seen returns the refund it made then."""
		if idempotency_key in self.refund_keys:
			return self.get_refund(self.refund_keys[idempotency_key])

		self.created_refunds.append({"payment_id": payment_id, "amount": amount})
		refund = RazorpayRefund(
			id=f"rfnd_{frappe.generate_hash(length=12)}", status=self.next_refund_status, amount=amount
		)
		self.refunds[refund.id] = refund
		if idempotency_key:
			self.refund_keys[idempotency_key] = refund.id
		if self.lose_next_refund_answer:
			type(self).lose_next_refund_answer = False
			frappe.throw(_("Razorpay could not be reached"), exc=GatewayUnavailableError)
		return refund.model_copy()

	def get_refund(self, refund_id: str) -> RazorpayRefund:
		if refund_id not in self.refunds:
			frappe.throw(_("Razorpay rejected the request: {0}").format("refund not found"))
		return self.refunds[refund_id].model_copy()


def sign_payload(payload: bytes, secret: str) -> str:
	return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def build_payment_link_event(link_id: str, status: str = "paid", event: str = "payment_link.paid") -> bytes:
	return json.dumps(
		{
			"entity": "event",
			"event": event,
			"payload": {"payment_link": {"entity": {"id": link_id, "status": status}}},
		}
	).encode()


def sign_checkout_payment(order_id: str, payment_id: str, key_secret: str) -> str:
	"""What Razorpay's checkout script hands the page as `razorpay_signature`."""
	return sign_payload(f"{order_id}|{payment_id}".encode(), key_secret)


def build_order_paid_event(order_id: str, payment_id: str, event: str = "order.paid") -> bytes:
	payment = {"id": payment_id, "order_id": order_id, "status": "captured"}
	payload = {"payment": {"entity": payment}}
	if event == "order.paid":
		payload["order"] = {"entity": {"id": order_id, "status": "paid"}}
	return json.dumps({"entity": "event", "event": event, "payload": payload}).encode()


def build_refund_event(
	refund_id: str, payment_id: str, status: str, amount: int, currency: str = "INR"
) -> bytes:
	return json.dumps(
		{
			"entity": "event",
			# Razorpay announces a refund that is still pending as refund.created.
			"event": "refund.created" if status == "pending" else f"refund.{status}",
			"payload": {
				"refund": {
					"entity": {
						"id": refund_id,
						"payment_id": payment_id,
						"status": status,
						"amount": amount,
						"currency": currency,
					}
				}
			},
		}
	).encode()
