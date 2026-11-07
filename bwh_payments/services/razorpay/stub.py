import hashlib
import hmac
import json
from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayEntity,
	RazorpayPayment,
	RazorpayPaymentDetails,
	RazorpayPaymentLink,
	RazorpayProduct,
	RazorpayRefund,
	RazorpayTransfer,
)

ACCEPTED_REFUND_STATUSES = ("processed", "pending")


class StubRazorpayClient(BaseRazorpayClient):
	"""In-memory Razorpay for tests. Call reset() in setUp."""

	links: ClassVar[dict[str, RazorpayPaymentLink]] = {}
	created_links: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	refunds: ClassVar[dict[str, RazorpayRefund]] = {}
	next_refund_status: ClassVar[str] = "processed"
	link_payloads: ClassVar[dict[str, dict]] = {}
	payments: ClassVar[dict[str, RazorpayPaymentDetails]] = {}
	transfers: ClassVar[dict[str, list[RazorpayTransfer]]] = {}
	created_reversals: ClassVar[list[dict]] = []
	updated_transfers: ClassVar[list[dict]] = []
	refuse_reversals: ClassVar[bool] = False
	# Each call is (method, account_id, payload): onboarding requests keep Razorpay's own shapes.
	account_calls: ClassVar[list[tuple]] = []
	products: ClassVar[dict[str, RazorpayProduct]] = {}

	@classmethod
	def reset(cls):
		cls.links = {}
		cls.created_links = []
		cls.created_refunds = []
		cls.refunds = {}
		cls.next_refund_status = "processed"
		cls.link_payloads = {}
		cls.payments = {}
		cls.transfers = {}
		cls.created_reversals = []
		cls.updated_transfers = []
		cls.refuse_reversals = False
		cls.account_calls = []
		cls.products = {}

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
		"""Capture a payment and close the link. Razorpay makes the link's transfers on capture."""
		cls.set_status(link_id, "paid")
		payment_id = cls.add_payment(link_id, "captured")
		payload = cls.link_payloads.get(link_id) or {}
		cls.payments[payment_id] = RazorpayPaymentDetails(id=payment_id, amount=payload.get("amount") or 0)
		cls.transfers[payment_id] = [
			build_transfer(transfer)
			for transfer in (payload.get("options") or {}).get("order", {}).get("transfers", [])
		]
		return payment_id

	@classmethod
	def set_activation_status(cls, product_id: str, status: str, requirements: list[dict] | None = None):
		cls.products[product_id].activation_status = status
		cls.products[product_id].requirements = requirements or []

	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink:
		self.created_links.append(payload)
		link_id = self.add_link()
		self.link_payloads[link_id] = payload
		return self.get_payment_link(link_id)

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
		if payment_id in self.payments and refund.status in ACCEPTED_REFUND_STATUSES:
			self.payments[payment_id].amount_refunded += amount
		return refund.model_copy()

	def get_payment(self, payment_id: str) -> RazorpayPaymentDetails:
		if payment_id not in self.payments:
			refuse("payment not found")
		return self.payments[payment_id].model_copy()

	def get_payment_transfers(self, payment_id: str) -> list[RazorpayTransfer]:
		return [transfer.model_copy() for transfer in self.transfers.get(payment_id, [])]

	def reverse_transfer(self, transfer_id: str, amount: int) -> None:
		transfer = self.find_transfer(transfer_id)
		if self.refuse_reversals or amount > transfer.amount - transfer.amount_reversed:
			refuse("the linked account balance is insufficient for this reversal")
		self.created_reversals.append({"transfer_id": transfer_id, "amount": amount})
		transfer.amount_reversed += amount
		transfer.status = "reversed" if transfer.amount_reversed == transfer.amount else "partially_reversed"

	def update_transfer(self, transfer_id: str, payload: dict) -> RazorpayTransfer:
		self.updated_transfers.append({"transfer_id": transfer_id, **payload})
		transfer = self.find_transfer(transfer_id)
		transfer.settlement_status = "on_hold" if payload.get("on_hold") else "pending"
		return transfer.model_copy()

	@classmethod
	def find_transfer(cls, transfer_id: str) -> RazorpayTransfer:
		"""The stored transfer itself, so a test can move its status or settlement."""
		for transfers in cls.transfers.values():
			for transfer in transfers:
				if transfer.id == transfer_id:
					return transfer
		refuse("transfer not found")

	def create_account(self, payload: dict) -> RazorpayEntity:
		return self.record_account_call("create_account", None, payload, "acc")

	def update_account(self, account_id: str, payload: dict) -> RazorpayEntity:
		self.account_calls.append(("update_account", account_id, payload))
		return RazorpayEntity(id=account_id)

	def create_stakeholder(self, account_id: str, payload: dict) -> RazorpayEntity:
		return self.record_account_call("create_stakeholder", account_id, payload, "sth")

	def update_stakeholder(self, account_id: str, stakeholder_id: str, payload: dict) -> RazorpayEntity:
		self.account_calls.append(("update_stakeholder", account_id, payload))
		return RazorpayEntity(id=stakeholder_id)

	def request_product(self, account_id: str, payload: dict) -> RazorpayProduct:
		product_id = self.record_account_call("request_product", account_id, payload, "acc_prd").id
		self.products[product_id] = RazorpayProduct(id=product_id, activation_status="requested")
		return self.products[product_id].model_copy()

	def update_product(self, account_id: str, product_id: str, payload: dict) -> RazorpayProduct:
		self.account_calls.append(("update_product", account_id, payload))
		self.products[product_id].activation_status = "under_review"
		return self.get_product(account_id, product_id)

	def get_product(self, account_id: str, product_id: str) -> RazorpayProduct:
		if product_id not in self.products:
			refuse("product configuration not found")
		return self.products[product_id].model_copy(deep=True)

	def record_account_call(self, method: str, account_id: str | None, payload: dict, prefix: str):
		self.account_calls.append((method, account_id, payload))
		return RazorpayEntity(id=f"{prefix}_{frappe.generate_hash(length=12)}")

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


def refuse(reason: str):
	frappe.throw(_("Razorpay rejected the request: {0}").format(reason))


def build_transfer(transfer: dict) -> RazorpayTransfer:
	return RazorpayTransfer(
		id=f"trf_{frappe.generate_hash(length=12)}",
		recipient=transfer["account"],
		amount=transfer["amount"],
		status="processed",
		settlement_status="on_hold" if transfer.get("on_hold") else "pending",
	)
