# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.base_class import (
	REUSABLE_CHECKOUT,
	CheckoutSession,
	PaymentGatewayBase,
	RefundResult,
	WebhookEvent,
)
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency
from bwh_payments.services.paypal.base import BasePayPalClient, PayPalOrder, PayPalOrderAlreadyCapturedError
from bwh_payments.services.paypal.live import LivePayPalClient
from bwh_payments.services.paypal.stub import StubPayPalClient

# PayPal settles in nothing else. SAR, AED, KWD, BHD, QAR and INR are notably absent.
PAYPAL_SUPPORTED_CURRENCIES = (
	"AUD",
	"CAD",
	"CHF",
	"CZK",
	"DKK",
	"EUR",
	"GBP",
	"HKD",
	"HUF",
	"ILS",
	"JPY",
	"MXN",
	"MYR",
	"NOK",
	"NZD",
	"PHP",
	"PLN",
	"SEK",
	"SGD",
	"THB",
	"TWD",
	"USD",
)

# Whole amounts only at PayPal, although HUF and TWD are two-decimal under ISO 4217.
PAYPAL_WHOLE_AMOUNT_CURRENCIES = ("HUF", "JPY", "TWD")

# Unmapped states stay Pending. APPROVED is absent: no money has moved until the capture.
PAYPAL_STATUS_MAP = {
	"COMPLETED": "Paid",
	"VOIDED": "Cancelled",
	"CREATED": "Pending",
	"SAVED": "Pending",
	"PAYER_ACTION_REQUIRED": "Pending",
}

PAYPAL_APPROVED_STATUS = "APPROVED"
PAYPAL_ACCEPTED_REFUND_STATUSES = ("COMPLETED", "PENDING")
# PayPal's refund statuses in the words Gateway Refund understands; anything else stays Pending.
PAYPAL_REFUND_STATUS_MAP = {
	"COMPLETED": "processed",
	"PENDING": "pending",
	"FAILED": "failed",
	"CANCELLED": "cancelled",
}
# The balance of a partially refunded capture stays refundable.
PAYPAL_REFUNDABLE_CAPTURE_STATUSES = ("COMPLETED", "PARTIALLY_REFUNDED")
# A fully refunded capture is still the payment that settled.
PAYPAL_SETTLED_CAPTURE_STATUSES = (*PAYPAL_REFUNDABLE_CAPTURE_STATUSES, "REFUNDED")
PAYPAL_VERIFICATION_SUCCESS = "SUCCESS"

PAYPAL_ORDER_APPROVED_EVENT = "CHECKOUT.ORDER.APPROVED"
PAYPAL_CAPTURE_COMPLETED_EVENT = "PAYMENT.CAPTURE.COMPLETED"

# `payer-action` answers an order created with `payment_source`; `approve` is the older spelling.
PAYPAL_REDIRECT_LINK_RELS = ("payer-action", "approve")

# Title case: `dict(frappe.request.headers)` drops werkzeug's case-insensitivity.
PAYPAL_SIGNATURE_HEADERS = {
	"auth_algo": "Paypal-Auth-Algo",
	"cert_url": "Paypal-Cert-Url",
	"transmission_id": "Paypal-Transmission-Id",
	"transmission_sig": "Paypal-Transmission-Sig",
	"transmission_time": "Paypal-Transmission-Time",
}


class PayPalGatewaySettings(Document, PaymentGatewayBase):
	description = "PayPal wallet payments. Not for INR or Gulf currencies."
	docs_url = "https://developer.paypal.com/dashboard/applications"
	# An approval link stays payable until the order is captured or expires.
	capabilities = (REUSABLE_CHECKOUT,)

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		brand_name: DF.Data | None
		cancelled_url: DF.Data | None
		client_id: DF.Data
		client_secret: DF.Password
		currency: DF.Link | None
		enabled: DF.Check
		failure_url: DF.Data | None
		mode: DF.Literal["Sandbox", "Live"]
		success_url: DF.Data | None
		webhook_id: DF.Data
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "PayPal"

	def supported_currencies(self) -> tuple[str, ...]:
		return PAYPAL_SUPPORTED_CURRENCIES

	def get_client(self) -> BasePayPalClient:
		if frappe.in_test:
			return StubPayPalClient()

		return LivePayPalClient(self.mode, self.client_id, self.get_password("client_secret"))

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
		validate_paypal_currency(currency)

		success_url = return_url or self.build_return_url(self.success_url, _("Success URL"), reference)
		cancel_url = return_url or self.build_return_url(self.cancelled_url, _("Cancelled URL"), reference)
		payload = {
			"intent": "CAPTURE",
			"purchase_units": [
				{
					# Echoed on captures and refunds, so a PayPal statement reconciles to the request.
					"custom_id": reference,
					"amount": {
						"currency_code": currency.upper(),
						"value": format_paypal_amount(amount, currency),
					},
				}
			],
			"payment_source": {
				"paypal": {
					"experience_context": {
						# No `locale`: PayPal wants BCP-47 and refuses a bare language tag with a 400.
						"user_action": "PAY_NOW",
						"return_url": success_url,
						"cancel_url": cancel_url,
						**({"brand_name": self.brand_name} if self.brand_name else {}),
					}
				}
			},
		}
		# No payer email: PayPal locks the wallet to it, and shoppers often use a different one there.

		order = self.get_client().create_order(payload, request_id=reference)
		# The order id, not a capture id: webhooks resolve back to the same `order_ref`.
		return CheckoutSession(
			session_id=order.id,
			redirect_url=get_redirect_url(order),
			success_url=success_url,
			cancel_url=cancel_url,
			failure_url=return_url or get_localised_url(self.failure_url),
		)

	def build_return_url(self, url: str, label: str, reference: str | None) -> str:
		"""PayPal appends only `token` and `PayerID`; the storefront reads `reference_id`."""
		if not url:
			frappe.throw(_("Please set the {0} in PayPal Gateway Settings").format(label))
		parts = urlsplit(get_localised_url(url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_payment_status(self, session_id: str) -> str:
		order = self.get_client().get_order(session_id)
		if read_status(order) == PAYPAL_APPROVED_STATUS:
			order = self.capture_order(session_id)

		return PAYPAL_STATUS_MAP.get(read_status(order), "Pending")

	def capture_order(self, session_id: str) -> PayPalOrder:
		"""Approval is not payment: without a capture the order sits approved and no money arrives."""
		client = self.get_client()
		try:
			client.capture_order(session_id)
		except PayPalOrderAlreadyCapturedError:
			# The return page and the APPROVED webhook race to capture; losing means the other one won.
			pass

		return client.get_order(session_id)

	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		event = self.verify_webhook(payload, headers)
		event_type = (event.get("event_type") or "").strip().upper()
		resource = event.get("resource") or {}

		if event_type == PAYPAL_ORDER_APPROVED_EVENT:
			session_id = resource.get("id")
			if not session_id:
				frappe.throw(_("PayPal sent a {0} event with no order id").format(event_type))
			# Charges a shopper who approved on PayPal and never came back to the storefront.
			status = PAYPAL_STATUS_MAP.get(read_status(self.capture_order(session_id)), "Pending")
		elif event_type == PAYPAL_CAPTURE_COMPLETED_EVENT:
			# `resource.id` is the capture; the order id is only in supplementary data.
			related_ids = (resource.get("supplementary_data") or {}).get("related_ids") or {}
			session_id = related_ids.get("order_id")
			if not session_id:
				frappe.throw(_("PayPal sent a {0} event with no order id").format(event_type))
			status = "Paid"
		else:
			return None

		return WebhookEvent(session_id=session_id, status=status, event_id=event.get("id"))

	def verify_webhook(self, payload: bytes, headers: dict) -> dict:
		"""PayPal signs with a certificate, so verification is a call back to PayPal with our webhook id."""
		if not self.webhook_id:
			frappe.throw(_("Webhook ID is not configured in PayPal Gateway Settings"))

		transmission = {}
		for field, header in PAYPAL_SIGNATURE_HEADERS.items():
			value = headers.get(header)
			if not value:
				frappe.throw(_("Missing {0} header").format(header))
			transmission[field] = value

		# parse_json passes bytes through untouched; a bad body raises ValueError either way.
		try:
			event = frappe.parse_json(payload.decode())
		except ValueError:
			frappe.throw(_("PayPal webhook body could not be read"))
		if not isinstance(event, dict):
			frappe.throw(_("PayPal webhook body was not an event"))

		verification = self.get_client().verify_webhook_signature(
			{**transmission, "webhook_id": self.webhook_id, "webhook_event": event}
		)
		if (verification.verification_status or "").strip().upper() != PAYPAL_VERIFICATION_SUCCESS:
			frappe.throw(_("PayPal could not verify this webhook delivery"))

		return event

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		currency = currency or self.currency
		client = self.get_client()
		capture_id = get_completed_capture_id(client.get_order(session_id))

		refund = client.refund_capture(
			capture_id,
			{
				"amount": {
					"currency_code": currency.upper(),
					"value": format_paypal_amount(amount, currency),
				}
			},
		)
		status = (refund.status or "").strip().upper()
		if status not in PAYPAL_ACCEPTED_REFUND_STATUSES:
			frappe.throw(_("Refund failed with status: {0}").format(refund.status))

		echoed = refund.amount.value if refund.amount else None
		return RefundResult(
			refund_id=refund.id,
			status=read_refund_status(refund),
			amount=flt(echoed) if echoed else flt(amount),
		)

	def get_refund_status(self, session_id: str, refund_id: str) -> RefundResult:
		refund = self.get_client().get_refund(refund_id)
		return RefundResult(refund_id=refund.id, status=read_refund_status(refund))

	def get_transaction_ref(self, session_id: str) -> str | None:
		"""The capture that collected the money: refunds go against it, not the order."""
		return find_capture_id(self.get_client().get_order(session_id), PAYPAL_SETTLED_CAPTURE_STATUSES)


def validate_paypal_currency(currency: str):
	if (currency or "").strip().upper() not in PAYPAL_SUPPORTED_CURRENCIES:
		frappe.throw(
			_("PayPal cannot take payment in {0}. It supports {1}.").format(
				frappe.bold(currency), ", ".join(PAYPAL_SUPPORTED_CURRENCIES)
			)
		)


def get_paypal_exponent(currency: str) -> int:
	if (currency or "").strip().upper() in PAYPAL_WHOLE_AMOUNT_CURRENCIES:
		return 0
	return get_minor_unit_exponent(currency)


def format_paypal_amount(amount, currency: str) -> str:
	"""Refused rather than rounded: sending 1000 for a 1000.50 HUF order bills a different figure."""
	exponent = get_paypal_exponent(currency)
	if flt(amount, get_minor_unit_exponent(currency)) != flt(amount, exponent):
		frappe.throw(
			_("PayPal does not accept decimals in {0}, so {1} cannot be charged.").format(
				frappe.bold(currency), amount
			)
		)
	return f"{flt(amount, exponent):.{exponent}f}"


def read_status(entity) -> str:
	return (entity.status or "").strip().upper()


def get_redirect_url(order: PayPalOrder) -> str:
	links = {link.rel: link.href for link in order.links or []}
	for rel in PAYPAL_REDIRECT_LINK_RELS:
		if links.get(rel):
			return links[rel]

	frappe.throw(_("PayPal created an order with no approval link"))


def get_completed_capture_id(order: PayPalOrder) -> str:
	"""An order is not a charge, so a refund goes against the capture that collected the money."""
	if capture_id := find_capture_id(order, PAYPAL_REFUNDABLE_CAPTURE_STATUSES):
		return capture_id

	frappe.throw(
		_(
			"PayPal has no completed capture for this order. An approved but uncaptured payment has to be"
			" captured or voided from the PayPal dashboard before it can be refunded."
		)
	)


def find_capture_id(order: PayPalOrder, statuses: tuple[str, ...]) -> str | None:
	for purchase_unit in order.purchase_units or []:
		for capture in (purchase_unit.payments.captures if purchase_unit.payments else None) or []:
			if capture.id and read_status(capture) in statuses:
				return capture.id
	return None


def read_refund_status(refund) -> str:
	return PAYPAL_REFUND_STATUS_MAP.get(read_status(refund), "pending")
