# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address
from frappe.utils.data import flt

from bwh_payments.base_class import CheckoutSession, PaymentGatewayBase, RefundResult, WebhookEvent
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import from_minor_units, to_minor_units, validate_transaction_currency
from bwh_payments.services.razorpay.base import BaseRazorpayClient, RazorpayPaymentLink
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

RAZORPAY_AUTHORISED_STATUS = "authorized"
RAZORPAY_CANCELLED_STATUS = "cancelled"
RAZORPAY_CAPTURED_STATUS = "captured"
RAZORPAY_PAID_EVENT = "payment_link.paid"
# Payments taken before Payment Links (e.g. imported from frappe/payments) are keyed on the payment id.
RAZORPAY_PAYMENT_ID_PREFIX = "pay_"
RAZORPAY_ACCEPTED_REFUND_STATUSES = ("processed", "pending")

# Title case: `dict(frappe.request.headers)` drops werkzeug's case-insensitivity.
RAZORPAY_SIGNATURE_HEADER = "X-Razorpay-Signature"
RAZORPAY_EVENT_ID_HEADER = "X-Razorpay-Event-Id"


class RazorpayGatewaySettings(Document, PaymentGatewayBase):
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
	) -> CheckoutSession:
		currency = currency or self.currency
		validate_transaction_currency(currency)

		notify = bool(self.send_razorpay_notifications)
		payload = {
			"amount": to_minor_units(amount, currency),
			"currency": currency.upper(),
			"reference_id": reference,
			"description": _("Online order payment"),
			"callback_url": self.build_success_url(reference),
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
			cancel_url=get_localised_url(self.cancelled_url),
			failure_url=get_localised_url(self.failure_url),
		)

	def build_success_url(self, reference: str | None) -> str:
		"""Razorpay sends `razorpay_payment_link_reference_id`; the storefront reads `reference_id`."""
		if not self.success_url:
			frappe.throw(_("Please set the Success URL in Razorpay Gateway Settings"))
		parts = urlsplit(get_localised_url(self.success_url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_payment_status(self, session_id: str) -> str:
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
			and has_authorised_payment(payment_link)
		):
			return "Paid"

		return status

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
		try:
			link = self.get_client().cancel_payment_link(session_id)
		except frappe.ValidationError:
			return False

		return (link.status or "").strip().casefold() == RAZORPAY_CANCELLED_STATUS

	def get_transaction_ref(self, session_id: str) -> str | None:
		payments = self.get_client().get_payment_link(session_id).payments or []
		for wanted in (RAZORPAY_CAPTURED_STATUS, RAZORPAY_AUTHORISED_STATUS):
			for payment in payments:
				if (payment.status or "").strip().casefold() == wanted:
					return payment.payment_id
		return None

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		currency = currency or self.currency
		client = self.get_client()
		if session_id.startswith(RAZORPAY_PAYMENT_ID_PREFIX):
			payment_id = session_id
		else:
			payment_id = get_captured_payment_id(client.get_payment_link(session_id))

		refund = client.create_refund(payment_id, to_minor_units(amount, currency))
		if refund.status not in RAZORPAY_ACCEPTED_REFUND_STATUSES:
			frappe.throw(_("Refund failed with status: {0}").format(refund.status))

		if refund.amount is not None:
			amount = flt(from_minor_units(refund.amount, currency))
		return RefundResult(refund_id=refund.id, status=refund.status, amount=amount)


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


def has_authorised_payment(payment_link: RazorpayPaymentLink) -> bool:
	return any(
		(payment.status or "").strip().casefold() == RAZORPAY_AUTHORISED_STATUS
		for payment in payment_link.payments or []
	)


def get_captured_payment_id(payment_link: RazorpayPaymentLink) -> str:
	for payment in payment_link.payments or []:
		if (payment.status or "").strip().casefold() == RAZORPAY_CAPTURED_STATUS:
			return payment.payment_id

	frappe.throw(
		_(
			"Razorpay has no captured payment for this link. An authorised but uncaptured payment has to be"
			" refunded from the Razorpay dashboard."
		)
	)
