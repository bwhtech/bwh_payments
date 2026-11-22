# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import hashlib
import hmac

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address
from frappe.utils.data import flt

from bwh_payments.base_class import (
	CheckoutSession,
	EmbeddedCheckout,
	PaymentGatewayBase,
	RefundResult,
	WebhookEvent,
)
from bwh_payments.bwh_payments.utils import add_reference_id, get_localised_url
from bwh_payments.currency import from_minor_units, to_minor_units, validate_transaction_currency
from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayPayment,
	RazorpayPaymentDetails,
)
from bwh_payments.services.razorpay.live import LiveRazorpayClient
from bwh_payments.services.razorpay.stub import StubRazorpayClient

# Unmapped states stay Pending.
RAZORPAY_LINK_STATUS_MAP = {
	"paid": "Paid",
	"created": "Pending",
	"partially_paid": "Pending",
	"expired": "Expired",
	"cancelled": "Cancelled",
}

# Razorpay `method` names that callers price by another name. Cards split by `card.type` instead.
RAZORPAY_PAYMENT_METHOD_MAP = {"cardless_emi": "emi"}
RAZORPAY_CARD_TYPES = ("credit", "debit")

RAZORPAY_AUTHORISED_STATUS = "authorized"
RAZORPAY_CANCELLED_STATUS = "cancelled"
RAZORPAY_CAPTURED_STATUS = "captured"
RAZORPAY_PAID_EVENT = "payment_link.paid"
RAZORPAY_ORDER_EVENTS = ("order.paid", "payment.captured")
RAZORPAY_PAID_ORDER_STATUS = "paid"
RAZORPAY_REFUND_EVENTS = ("refund.created", "refund.processed", "refund.failed")
# Payments taken before Payment Links (e.g. imported from frappe/payments) are keyed on the payment id.
RAZORPAY_PAYMENT_ID_PREFIX = "pay_"
# Embedded checkouts are keyed on a Razorpay order, and pay through Standard Checkout's script.
RAZORPAY_ORDER_ID_PREFIX = "order_"
RAZORPAY_CHECKOUT_SCRIPT = "https://checkout.razorpay.com/v1/checkout.js"
RAZORPAY_ACCEPTED_REFUND_STATUSES = ("processed", "pending")

# Title case: `dict(frappe.request.headers)` drops werkzeug's case-insensitivity.
RAZORPAY_SIGNATURE_HEADER = "X-Razorpay-Signature"
RAZORPAY_EVENT_ID_HEADER = "X-Razorpay-Event-Id"


class RazorpayGatewaySettings(Document, PaymentGatewayBase):
	logo = "/assets/bwh_payments/images/gateways/razorpay.svg"
	logo_dark = "/assets/bwh_payments/images/gateways/razorpay-dark.svg"
	description = "Cards, UPI, netbanking and wallets. India."
	docs_url = "https://dashboard.razorpay.com/app/website-app-settings/webhooks"
	supports_embedded_checkout = True
	idempotent_refunds = True

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cancelled_url: DF.Data | None
		currency: DF.Link | None
		enabled: DF.Check
		failure_url: DF.Data | None
		key_id: DF.Data
		key_secret: DF.Password
		send_razorpay_notifications: DF.Check
		success_url: DF.Data | None
		test_mode: DF.Check
		treat_authorised_as_paid: DF.Check
		webhook_secret: DF.Password | None
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "Razorpay"

	def get_client(self) -> BaseRazorpayClient:
		if frappe.in_test:
			return StubRazorpayClient()

		return LiveRazorpayClient(self.key_id, self.get_password("key_secret"))

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
		description: str | None = None,
	) -> CheckoutSession:
		currency = currency or self.currency
		validate_transaction_currency(currency)

		notify = bool(self.send_razorpay_notifications)
		payload = {
			"amount": to_minor_units(amount, currency),
			"currency": currency.upper(),
			"reference_id": reference,
			"description": description or _("Online order payment"),
			"callback_url": return_url or self.build_success_url(reference),
			"callback_method": "get",
			# Explicit, so the account's billable SMS/email defaults do not apply.
			"notify": {"sms": notify, "email": notify},
			"reminder_enable": notify,
			"notes": {"reference_id": reference or ""},
		}
		if customer_details := get_customer_details(customer or {}):
			payload["customer"] = customer_details

		link = self.get_client().create_payment_link(payload)
		# A Payment Link has one callback; cancel and failure URLs are only for the storefront.
		return CheckoutSession(
			session_id=link.id,
			redirect_url=link.short_url,
			success_url=payload["callback_url"],
			cancel_url=return_url or get_localised_url(self.cancelled_url),
			failure_url=return_url or get_localised_url(self.failure_url),
		)

	def create_embedded_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		description: str | None = None,
	) -> EmbeddedCheckout:
		currency = (currency or self.currency).upper()
		validate_transaction_currency(currency)
		payload = {
			"amount": to_minor_units(amount, currency),
			"currency": currency,
			"receipt": reference,
			"notes": {"reference_id": reference or ""},
		}
		order = self.get_client().create_order(payload)
		return EmbeddedCheckout(
			session_id=order.id,
			script_url=RAZORPAY_CHECKOUT_SCRIPT,
			options=self.build_checkout_options(order.id, payload, description, customer or {}),
		)

	def build_checkout_options(
		self, order_id: str, order: dict, description: str | None, customer: dict
	) -> dict:
		options = {
			"key": self.key_id,
			"order_id": order_id,
			"amount": order["amount"],
			"currency": order["currency"],
			"name": frappe.get_website_settings("app_name") or _("Payment"),
			"description": description or _("Online order payment"),
		}
		# Razorpay's prefill takes the same keys as a payment link's customer.
		if prefill := get_customer_details(customer):
			options["prefill"] = prefill
		return options

	def verify_embedded_payment(self, session_id: str, payload: dict) -> str:
		payment_id = payload.get("razorpay_payment_id")
		signature = payload.get("razorpay_signature")
		if not isinstance(payment_id, str) or not isinstance(signature, str):
			frappe.throw(_("Razorpay checkout did not return a payment"))

		# Signed over our own order id, never the one the browser sends back.
		message = f"{session_id}|{payment_id}".encode()
		expected = hmac.new(self.get_password("key_secret").encode(), message, hashlib.sha256).hexdigest()
		if not signature.isascii() or not hmac.compare_digest(expected, signature):
			frappe.throw(_("Razorpay checkout signature does not match"))

		return self.get_payment_status(session_id)

	def build_success_url(self, reference: str | None) -> str:
		"""Razorpay sends `razorpay_payment_link_reference_id`; the storefront reads `reference_id`."""
		if not self.success_url:
			frappe.throw(_("Please set the Success URL in Razorpay Gateway Settings"))
		return add_reference_id(self.success_url, reference)

	def get_payment_status(self, session_id: str) -> str:
		if is_order(session_id):
			return self.get_order_status(session_id)

		payment_link = self.get_client().get_payment_link(session_id)
		link_status = (payment_link.status or "").strip().casefold()
		status = RAZORPAY_LINK_STATUS_MAP.get(link_status, "Pending")
		if status != "Pending":
			return status

		# The link does not say whether the account auto-captures, hence the opt-in switch.
		# `partially_paid` also maps to Pending, so only an open link qualifies.
		if (
			link_status == "created"
			and self.treat_authorised_as_paid
			and has_authorised_payment(payment_link.payments)
		):
			return "Paid"

		return status

	def get_order_status(self, order_id: str) -> str:
		"""An order is paid once its payment is captured. It never expires or cancels."""
		client = self.get_client()
		if (client.get_order(order_id).status or "").strip().casefold() == RAZORPAY_PAID_ORDER_STATUS:
			return "Paid"
		if self.treat_authorised_as_paid and has_authorised_payment(client.get_order_payments(order_id)):
			return "Paid"
		return "Pending"

	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		webhook_secret = self.get_password("webhook_secret")
		if not webhook_secret:
			frappe.throw(_("Webhook secret is not configured in Razorpay Gateway Settings"))

		signature = headers.get(RAZORPAY_SIGNATURE_HEADER)
		if not signature:
			frappe.throw(_("Missing {0} header").format(RAZORPAY_SIGNATURE_HEADER))

		# Replay protection rests on this header; the body has no event id.
		event_id = headers.get(RAZORPAY_EVENT_ID_HEADER)
		if not event_id:
			frappe.throw(_("Missing {0} header").format(RAZORPAY_EVENT_ID_HEADER))

		# Signed over the raw bytes with the webhook secret, not `key_secret`.
		expected_signature = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
		# compare_digest raises TypeError on a non-ASCII str.
		if not signature.isascii() or not hmac.compare_digest(expected_signature, signature):
			frappe.throw(_("Razorpay webhook signature does not match"))

		# parse_json passes bytes through untouched.
		event = frappe.parse_json(payload.decode())
		if event.get("event") in RAZORPAY_REFUND_EVENTS:
			return read_refund_event(event, event_id)
		if event.get("event") in RAZORPAY_ORDER_EVENTS:
			return read_order_event(event, event_id)
		if event.get("event") != RAZORPAY_PAID_EVENT:
			return None

		payment_link = ((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
		session_id = payment_link.get("id")
		status = RAZORPAY_LINK_STATUS_MAP.get(
			(payment_link.get("status") or "").strip().casefold(), "Pending"
		)
		# This event means paid, so an unreadable entity must 400 and be retried, not 200 and lost.
		if not session_id or status != "Paid":
			frappe.throw(_("Unreadable payment link entity on a {0} event").format(RAZORPAY_PAID_EVENT))

		# The link id, not the payment id: `order_ref` holds the link id.
		return WebhookEvent(session_id=session_id, status=status, event_id=event_id)

	def cancel_session(self, session_id: str) -> bool:
		# Razorpay cannot cancel an order.
		if is_order(session_id):
			return False
		try:
			link = self.get_client().cancel_payment_link(session_id)
		except frappe.ValidationError:
			return False

		return (link.status or "").strip().casefold() == RAZORPAY_CANCELLED_STATUS

	def get_refund_status(self, session_id: str, refund_id: str) -> RefundResult:
		refund = self.get_client().get_refund(refund_id)
		return RefundResult(refund_id=refund.id, status=refund.status)

	def get_transaction_ref(self, session_id: str) -> str | None:
		payments = self.get_session_payments(session_id)
		for wanted in (RAZORPAY_CAPTURED_STATUS, RAZORPAY_AUTHORISED_STATUS):
			for payment in payments:
				if (payment.status or "").strip().casefold() == wanted:
					return payment.payment_id
		return None

	def get_payment_method(self, session_id: str, transaction_ref: str | None = None) -> str | None:
		payment_id = transaction_ref or self.find_payment_id(session_id)
		if not payment_id:
			return None
		return read_payment_method(self.get_client().get_payment(payment_id))

	def get_session_payments(self, session_id: str) -> list[RazorpayPayment]:
		client = self.get_client()
		if is_order(session_id):
			return client.get_order_payments(session_id)
		return client.get_payment_link(session_id).payments or []

	def find_payment_id(self, session_id: str) -> str | None:
		if session_id.startswith(RAZORPAY_PAYMENT_ID_PREFIX):
			return session_id
		return self.get_transaction_ref(session_id)

	def refund_payment(
		self,
		session_id: str,
		amount: float,
		currency: str | None = None,
		idempotency_key: str | None = None,
	) -> RefundResult:
		currency = currency or self.currency
		if session_id.startswith(RAZORPAY_PAYMENT_ID_PREFIX):
			payment_id = session_id
		else:
			payment_id = get_captured_payment_id(self.get_session_payments(session_id))

		refund = self.get_client().create_refund(
			payment_id, to_minor_units(amount, currency), idempotency_key=idempotency_key
		)
		if refund.status not in RAZORPAY_ACCEPTED_REFUND_STATUSES:
			frappe.throw(_("Refund failed with status: {0}").format(refund.status))

		if refund.amount is not None:
			amount = flt(from_minor_units(refund.amount, currency))
		return RefundResult(refund_id=refund.id, status=refund.status, amount=amount)


def read_refund_event(event: dict, event_id: str) -> WebhookEvent:
	refund = ((event.get("payload") or {}).get("refund") or {}).get("entity") or {}
	currency = refund.get("currency") or "INR"
	return WebhookEvent(
		refund_id=refund.get("id"),
		status=refund.get("status") or "",
		refund_amount=from_minor_units(refund.get("amount") or 0, currency),
		transaction_ref=refund.get("payment_id"),
		event_id=event_id,
	)


def read_order_event(event: dict, event_id: str) -> WebhookEvent | None:
	"""A payment link makes its own order too, so only an order one of our requests holds is read."""
	payload = event.get("payload") or {}
	payment = (payload.get("payment") or {}).get("entity") or {}
	order_id = payment.get("order_id") or ((payload.get("order") or {}).get("entity") or {}).get("id")
	if not order_id or not frappe.db.exists("Gateway Payment Request", {"order_ref": order_id}):
		return None
	return WebhookEvent(
		session_id=order_id, status="Paid", event_id=event_id, transaction_ref=payment.get("id")
	)


def is_order(session_id: str) -> bool:
	return session_id.startswith(RAZORPAY_ORDER_ID_PREFIX)


def read_payment_method(payment: RazorpayPaymentDetails) -> str | None:
	method = (payment.method or "").strip().casefold()
	if method != "card":
		return RAZORPAY_PAYMENT_METHOD_MAP.get(method, method or None)

	card_type = ((payment.card and payment.card.type) or "").strip().casefold()
	return f"card_{card_type}" if card_type in RAZORPAY_CARD_TYPES else "card"


def get_customer_details(customer: dict) -> dict:
	"""Razorpay rejects a blank `contact` or `email`, so empty values are dropped."""
	details = {
		"name": get_customer_name(customer),
		"email": validate_email_address(customer.get("email") or "", throw=False),
		"contact": customer.get("phone"),
	}
	return {key: value for key, value in details.items() if value}


def get_customer_name(customer: dict) -> str:
	name = customer.get("name") or {}
	return " ".join(part for part in (name.get("forenames"), name.get("surname")) if part)


def has_authorised_payment(payments: list[RazorpayPayment] | None) -> bool:
	return any(
		(payment.status or "").strip().casefold() == RAZORPAY_AUTHORISED_STATUS for payment in payments or []
	)


def get_captured_payment_id(payments: list[RazorpayPayment]) -> str:
	for payment in payments:
		if (payment.status or "").strip().casefold() == RAZORPAY_CAPTURED_STATUS:
			return payment.payment_id

	frappe.throw(
		_(
			"Razorpay has no captured payment for this checkout. An authorised but uncaptured payment has to be"
			" refunded from the Razorpay dashboard."
		)
	)
