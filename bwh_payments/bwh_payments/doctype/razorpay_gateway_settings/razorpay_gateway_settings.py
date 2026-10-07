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

# Anything unrecognised stays Pending: never terminal, and never Paid, so an unmapped Razorpay state can
# neither release goods nor cancel a live order. Only values the webhook/poll path may write appear here.
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
RAZORPAY_ACCEPTED_REFUND_STATUSES = ("processed", "pending")

# `dict(frappe.request.headers)` loses werkzeug's case-insensitivity, so these have to match the
# canonical title-case werkzeug hands over, not Razorpay's documented lowercase spelling.
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
		return (
			StubRazorpayClient()
			if frappe.in_test
			else LiveRazorpayClient(self.key_id, self.get_password("key_secret"))
		)

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
			# Razorpay's defaults SMS and email the link on every checkout and chase abandoned carts, all
			# billable, from a sender the storefront never named — and the shopper is already looking at
			# the redirect page. Sent explicitly so the account's defaults cannot decide this for us.
			"notify": {"sms": notify, "email": notify},
			"reminder_enable": notify,
			"notes": {"reference_id": reference or ""},
		}
		# Razorpay validates every key it is given: a blank `contact` or `email` is a hard 400, which would
		# make checkout impossible for a guest who left them empty.
		if customer_details := get_customer_details(customer or {}):
			payload["customer"] = customer_details

		link = self.get_client().create_payment_link(payload)
		# A Payment Link carries a single callback, so Razorpay has no cancel or failure redirect of its
		# own; the storefront gets ours instead, which is what the shopper would have landed on anyway.
		return CheckoutSession(
			session_id=link.id,
			redirect_url=link.short_url,
			success_url=payload["callback_url"],
			cancel_url=get_localised_url(self.cancelled_url),
			failure_url=get_localised_url(self.failure_url),
		)

	def build_success_url(self, reference: str | None) -> str:
		"""Razorpay's callback names its parameter `razorpay_payment_link_reference_id`, but the
		storefront confirmation page reads `reference_id`, so ours is appended alongside it."""
		if not self.success_url:
			frappe.throw(_("Please set the Success URL in Razorpay Gateway Settings"))
		# String-concatenating "?reference_id=..." breaks any success URL that already carries a query.
		parts = urlsplit(get_localised_url(self.success_url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_payment_status(self, session_id: str) -> str:
		payment_link = self.get_client().get_payment_link(session_id)
		# Normalised once: reading the raw field again below would let a " Created " past the map but not
		# past the eligibility check, so the two reads have to agree.
		link_status = (payment_link.status or "").strip().casefold()
		status = RAZORPAY_LINK_STATUS_MAP.get(link_status, "Pending")
		if status != "Pending":
			return status

		# Whether an authorisation is money in the bank depends on how the Razorpay account captures, and
		# the link does not report which. With auto-capture, leaving an authorised payment Pending strands
		# paid orders forever; with manual capture, calling it Paid ships goods against uncaptured funds.
		# Hence the per-account switch, defaulting to the safe side. Only an open link is eligible:
		# `partially_paid` also maps to Pending, and part of the money is not the money.
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

		# Razorpay's body has no top-level event id, so replay protection rests entirely on this header.
		# Accepting a delivery without it would silently disable that guard.
		event_id = headers.get(RAZORPAY_EVENT_ID_HEADER)
		if not event_id:
			frappe.throw(_("Missing {0} header").format(RAZORPAY_EVENT_ID_HEADER))

		# Razorpay signs the exact bytes it sent with the webhook secret, not the API key secret. Hashing
		# a re-serialised body, or reaching for `key_secret`, fails every delivery for no visible reason.
		expected_signature = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
		# compare_digest raises TypeError on a non-ASCII str, which would surface as an unexplained
		# TypeError instead of the mismatch this actually is.
		if not signature.isascii() or not hmac.compare_digest(expected_signature, signature):
			frappe.throw(_("Razorpay webhook signature does not match"))

		# parse_json passes bytes straight through untouched, so the body is decoded first.
		event = frappe.parse_json(payload.decode())
		if event.get("event") != RAZORPAY_PAID_EVENT:
			return None

		payment_link = ((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
		session_id = payment_link.get("id")
		status = RAZORPAY_LINK_STATUS_MAP.get(
			(payment_link.get("status") or "").strip().casefold(), "Pending"
		)
		# Razorpay only sends this event once the link is paid, so an entity we cannot read is a paid order
		# we would otherwise leave Pending with a 200 and no retry. Throwing turns it into a 400 that
		# Razorpay redelivers and that lands in the Error Log.
		if not session_id or status != "Paid":
			frappe.throw(_("Unreadable payment link entity on a {0} event").format(RAZORPAY_PAID_EVENT))

		# The payment link id, not the payment id: `order_ref` holds the link id, and the webhook handler
		# matches on it byte-for-byte.
		return WebhookEvent(session_id=session_id, status=status, event_id=event_id)

	def cancel_session(self, session_id: str) -> bool:
		try:
			link = self.get_client().cancel_payment_link(session_id)
		except frappe.ValidationError:
			return False

		return (link.status or "").strip().casefold() == RAZORPAY_CANCELLED_STATUS

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		currency = currency or self.currency
		client = self.get_client()
		payment_id = get_captured_payment_id(client.get_payment_link(session_id))

		refund = client.create_refund(payment_id, to_minor_units(amount, currency))
		if refund.status not in RAZORPAY_ACCEPTED_REFUND_STATUSES:
			frappe.throw(_("Refund failed with status: {0}").format(refund.status))

		# The gateway's own echo is round-tripped back to major units so a charge and its refund always
		# agree to the last minor unit.
		return RefundResult(
			refund_id=refund.id, status=refund.status, amount=flt(from_minor_units(refund.amount, currency))
		)


def get_customer_details(customer: dict) -> dict:
	"""Only the values Razorpay will accept: it rejects a blank `contact` or `email` outright."""
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
	"""A Payment Link is not a charge, so a refund has to go against the payment it collected."""
	# ponytail: a request marked Paid through `treat_authorised_as_paid` holds an authorised-but-uncaptured
	# payment and cannot be refunded here; capture it first, or refund it from the Razorpay dashboard.
	for payment in payment_link.payments or []:
		if (payment.status or "").strip().casefold() == RAZORPAY_CAPTURED_STATUS:
			return payment.payment_id

	frappe.throw(
		_(
			"Razorpay has no captured payment for this link. An authorised but uncaptured payment has to be"
			" refunded from the Razorpay dashboard."
		)
	)
