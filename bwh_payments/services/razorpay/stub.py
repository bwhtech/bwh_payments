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
	"""In-memory Razorpay, selected under tests. Drive outcomes through the classmethod controls; reset()
	between tests. Recordings keep Razorpay's own request shapes."""

	links: ClassVar[dict[str, RazorpayPaymentLink]] = {}
	created_links: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	next_refund_status: ClassVar[str] = "processed"

	@classmethod
	def reset(cls):
		cls.links = {}
		cls.created_links = []
		cls.created_refunds = []
		cls.next_refund_status = "processed"

	# --- controls -----------------------------------------------------------------------------------

	@classmethod
	def add_link(cls, status: str = "created") -> str:
		"""A link that exists at Razorpay without this app having created it."""
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
		"""The shopper completes the hosted page: Razorpay captures a payment and closes the link."""
		cls.set_status(link_id, "paid")
		return cls.add_payment(link_id, "captured")

	# --- API ----------------------------------------------------------------------------------------

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
		return RazorpayRefund(
			id=f"rfnd_{frappe.generate_hash(length=12)}", status=self.next_refund_status, amount=amount
		)


def sign_payload(payload: bytes, secret: str) -> str:
	"""A genuine X-Razorpay-Signature, so tests exercise signature verification instead of bypassing it."""
	return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def build_payment_link_event(link_id: str, status: str = "paid", event: str = "payment_link.paid") -> bytes:
	return json.dumps(
		{
			"entity": "event",
			"event": event,
			"payload": {"payment_link": {"entity": {"id": link_id, "status": status}}},
		}
	).encode()
