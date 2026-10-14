# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import hashlib
import hmac

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.base_class import CheckoutSession, PaymentGatewayBase, RefundResult, WebhookEvent
from bwh_payments.bwh_payments.utils import add_reference_id, get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency
from bwh_payments.services.tabby.base import BaseTabbyClient, TabbyCheckout, TabbyConfiguration, TabbyPayment
from bwh_payments.services.tabby.live import LiveTabbyClient
from bwh_payments.services.tabby.stub import StubTabbyClient

# Tabby only underwrites in these markets; anything else is a 400 at checkout.
TABBY_SUPPORTED_CURRENCIES = ("SAR", "AED", "KWD", "BHD", "QAR")

# Unmapped states stay Pending. `authorized` is absent on purpose: no money has moved until capture.
TABBY_STATUS_MAP = {
	"closed": "Paid",
	"rejected": "Not Paid",
	"expired": "Expired",
	"created": "Pending",
}

TABBY_AUTHORISED_STATUS = "authorized"
TABBY_CAPTURED_STATUS = "closed"
TABBY_REJECTED_STATUS = "rejected"

# Title case: `dict(frappe.request.headers)` drops werkzeug's case-insensitivity.
TABBY_SIGNATURE_HEADER = "X-Webhook-Signature"

TABBY_REJECTION_MESSAGES = {
	"not_available": "Tabby cannot approve this order right now. Please choose another payment method.",
	"order_amount_too_high": "This order is above Tabby's limit. Please choose another payment method.",
	"order_amount_too_low": "This order is below Tabby's minimum. Please choose another payment method.",
}
DEFAULT_REJECTION_MESSAGE = "Tabby could not approve this order. Please choose another payment method."


class TabbyGatewaySettings(Document, PaymentGatewayBase):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cancelled_url: DF.Data | None
		currency: DF.Link | None
		enabled: DF.Check
		failure_url: DF.Data | None
		key_secret: DF.Password
		merchant_code: DF.Data
		success_url: DF.Data | None
		webhook_ips: DF.SmallText | None
		webhook_secret: DF.Password
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "Tabby"

	def get_client(self) -> BaseTabbyClient:
		if frappe.in_test:
			return StubTabbyClient()

		return LiveTabbyClient(self.get_password("key_secret"), self.merchant_code)

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
	) -> CheckoutSession:
		currency = currency or self.currency
		validate_transaction_currency(currency)
		validate_tabby_currency(currency)

		if return_url:
			merchant_urls = dict.fromkeys(("success", "cancel", "failure"), return_url)
		else:
			merchant_urls = {
				"success": self.build_return_url(self.success_url, _("Success URL"), reference),
				"cancel": self.build_return_url(self.cancelled_url, _("Cancelled URL"), reference),
				"failure": self.build_return_url(self.failure_url, _("Failure URL"), reference),
			}
		payload = {
			"payment": {
				"amount": format_tabby_amount(amount, currency),
				"currency": currency.upper(),
				# Empty values are dropped: Tabby rejects keys it cannot read.
				"buyer": get_buyer_details(customer or {}),
				"shipping_address": get_shipping_address(customer or {}),
				"order": {"reference_id": reference},
			},
			"lang": frappe.local.lang,
			"merchant_code": self.merchant_code,
			"merchant_urls": merchant_urls,
		}

		checkout = self.get_client().create_checkout(payload)
		redirect_url = get_installments_url(checkout)

		# The payment id, not the checkout id: status, capture, refund and webhooks all key on it.
		session_id = checkout.payment.id if checkout.payment else None
		if not session_id:
			frappe.throw(_("Tabby returned a checkout without a payment id"))

		return CheckoutSession(
			session_id=session_id,
			redirect_url=redirect_url,
			success_url=merchant_urls["success"],
			cancel_url=merchant_urls["cancel"],
			failure_url=merchant_urls["failure"],
		)

	def build_return_url(self, url: str, label: str, reference: str | None) -> str:
		"""Tabby only appends `?payment_id=...` on return, so add our request name."""
		if not url:
			frappe.throw(_("Please set the {0} in Tabby Gateway Settings").format(label))
		return add_reference_id(url, reference)

	def get_payment_status(self, session_id: str) -> str:
		client = self.get_client()
		payment = client.get_payment(session_id)
		status = read_status(payment.status)

		if status == TABBY_AUTHORISED_STATUS:
			payment = self.capture_payment(client, session_id, payment)
			status = read_status(payment.status)

		return TABBY_STATUS_MAP.get(status, "Pending")

	def capture_payment(
		self, client: BaseTabbyClient, session_id: str, payment: TabbyPayment
	) -> TabbyPayment:
		"""This is where the shopper is charged. A failed capture raises, so the request stays Pending."""
		# The authorised amount, never a caller's figure, so a capture cannot exceed what Tabby approved.
		currency = payment.currency or self.currency
		client.capture_payment(session_id, format_tabby_amount(payment.amount, currency))
		return client.get_payment(session_id)

	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		self.validate_webhook_source_ip()

		webhook_secret = self.get_password("webhook_secret")
		if not webhook_secret:
			frappe.throw(_("Webhook secret is not configured in Tabby Gateway Settings"))

		# Tabby does not sign webhooks; this header is a static token. Compare in constant time.
		signature = headers.get(TABBY_SIGNATURE_HEADER)
		if not signature:
			frappe.throw(_("Missing {0} header").format(TABBY_SIGNATURE_HEADER))
		# compare_digest raises TypeError on a non-ASCII str.
		if not signature.isascii() or not hmac.compare_digest(webhook_secret, signature):
			frappe.throw(_("Tabby webhook token does not match"))

		# parse_json passes bytes through untouched. Decode and JSON errors are both ValueError.
		try:
			payment = frappe.parse_json(payload.decode())
		except ValueError:
			return None
		if not isinstance(payment, dict):
			return None

		session_id = payment.get("id")
		if not session_id:
			frappe.throw(_("Tabby webhook carried no payment id"))

		# `authorized` stays Pending: capture moves money, so it waits for a status sync, not a Guest webhook.
		return WebhookEvent(
			session_id=session_id,
			status=TABBY_STATUS_MAP.get(read_status(payment.get("status")), "Pending"),
			event_id=get_webhook_event_id(payment),
		)

	def validate_webhook_source_ip(self):
		"""Defence in depth on top of the shared token."""
		allowed_ips = [line.strip() for line in (self.webhook_ips or "").splitlines() if line.strip()]
		if not allowed_ips:
			return
		if frappe.local.request_ip not in allowed_ips:
			frappe.throw(_("Tabby webhook came from an address that is not allowed"))

	def supported_currencies(self) -> tuple[str, ...]:
		return TABBY_SUPPORTED_CURRENCIES

	def get_transaction_ref(self, session_id: str) -> str | None:
		# Tabby's session id is already the payment id.
		return session_id

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		currency = currency or self.currency
		client = self.get_client()
		if read_status(client.get_payment(session_id).status) != TABBY_CAPTURED_STATUS:
			frappe.throw(
				_(
					"Tabby has not captured this payment yet, so it cannot be refunded. An authorised but"
					" uncaptured payment has to be voided from the Tabby dashboard."
				)
			)

		# Tabby returns the updated payment; the new refund is its last entry.
		refunds = client.refund_payment(session_id, format_tabby_amount(amount, currency)).refunds or []
		if not refunds:
			frappe.throw(_("Tabby accepted the refund but returned no refund record"))

		# Tabby has no per-refund status; an accepted refund has succeeded.
		return RefundResult(refund_id=refunds[-1].id, status="succeeded", amount=flt(amount))


def validate_tabby_currency(currency: str):
	if (currency or "").strip().upper() not in TABBY_SUPPORTED_CURRENCIES:
		frappe.throw(
			_("Tabby cannot take payment in {0}. It supports {1}.").format(
				frappe.bold(currency), ", ".join(TABBY_SUPPORTED_CURRENCIES)
			)
		)


def format_tabby_amount(amount, currency: str) -> str:
	"""Major units as a decimal string at the currency's exponent (KWD and BHD carry three)."""
	exponent = get_minor_unit_exponent(currency)
	return f"{flt(amount, exponent):.{exponent}f}"


def read_status(status: str | None) -> str:
	return (status or "").strip().casefold()


def get_buyer_details(customer: dict) -> dict:
	details = {
		"phone": customer.get("phone"),
		"email": customer.get("email"),
		"name": get_customer_name(customer),
	}
	return {key: value for key, value in details.items() if value}


def get_customer_name(customer: dict) -> str:
	name = customer.get("name") or {}
	return " ".join(part for part in (name.get("forenames"), name.get("surname")) if part)


def get_shipping_address(customer: dict) -> dict:
	address = customer.get("address") or {}
	details = {"address": address.get("line1"), "city": address.get("city")}
	return {key: value for key, value in details.items() if value}


def get_installments_url(checkout: TabbyCheckout) -> str:
	configuration = checkout.configuration or TabbyConfiguration()
	available_products = configuration.available_products
	installments = (available_products.installments if available_products else None) or []
	if read_status(checkout.status) == TABBY_REJECTED_STATUS or not installments:
		throw_rejection(configuration)

	web_url = installments[0].web_url
	if not web_url:
		throw_rejection(configuration)
	return web_url


def throw_rejection(configuration: TabbyConfiguration):
	product_status = configuration.products.installments if configuration.products else None
	reason = ((product_status.rejection_reason if product_status else None) or "").strip().casefold()
	frappe.throw(_(TABBY_REJECTION_MESSAGES.get(reason, DEFAULT_REJECTION_MESSAGE)), title=_("Tabby"))


def get_webhook_event_id(payment: dict) -> str:
	"""Tabby sends no event id, so derive one for replay protection."""
	timestamp = payment.get("updated_at") or payment.get("created_at") or ""
	fingerprint = f"{payment.get('id')}|{payment.get('status')}|{timestamp}"
	return hashlib.sha256(fingerprint.encode()).hexdigest()
