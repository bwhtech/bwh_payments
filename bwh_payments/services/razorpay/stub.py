import hashlib
import hmac
import json
from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayPayment,
	RazorpayPaymentLink,
	RazorpayRefund,
)


class StubRazorpayClient(BaseRazorpayClient):
	"""In-memory Razorpay for tests. Call reset() in setUp."""

	links: ClassVar[dict[str, RazorpayPaymentLink]] = {}
	created_links: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	refunds: ClassVar[dict[str, RazorpayRefund]] = {}
	next_refund_status: ClassVar[str] = "processed"

	@classmethod
	def reset(cls):
		cls.links = {}
		cls.created_links = []
		cls.created_refunds = []
		cls.refunds = {}
		cls.next_refund_status = "processed"

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
	def set_status(cls, link_id: str, status: str):
		cls.links[link_id].status = status

	@classmethod
	def add_payment(cls, link_id: str, status: str) -> str:
		payment_id = f"pay_{frappe.generate_hash(length=12)}"
		cls.links[link_id].payments.append(RazorpayPayment(payment_id=payment_id, status=status))
		return payment_id

	@classmethod
	def pay(cls, link_id: str) -> str:
		"""Capture a payment and close the link."""
		cls.set_status(link_id, "paid")
		return cls.add_payment(link_id, "captured")

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

	def create_refund(self, payment_id: str, amount: int) -> RazorpayRefund:
		self.created_refunds.append({"payment_id": payment_id, "amount": amount})
		refund = RazorpayRefund(
			id=f"rfnd_{frappe.generate_hash(length=12)}", status=self.next_refund_status, amount=amount
		)
		self.refunds[refund.id] = refund
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


def build_refund_event(
	refund_id: str, payment_id: str, status: str, amount: int, currency: str = "INR"
) -> bytes:
	return json.dumps(
		{
			"entity": "event",
			"event": f"refund.{status}",
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
