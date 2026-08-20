# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.base_class import PaymentGatewayBase
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency

# ponytail: frappe.integrations.utils.make_request takes no timeout, so a hung Tabby call holds a worker;
# revisit if Tabby latency ever shows up in the request log.
TABBY_BASE_URL = "https://api.tabby.ai"

# Tabby only underwrites in these markets, and a merchant code is tied to one of them. Sending anything
# else is a 400 the shopper sees as a broken checkout, so it is refused before the call is made.
TABBY_SUPPORTED_CURRENCIES = ("SAR", "AED", "KWD", "BHD", "QAR")

# Tabby reports status in lowercase. Anything unrecognised stays Pending: never terminal, and never Paid,
# so an unmapped Tabby state can neither release goods nor cancel a live order. `authorized` is
# deliberately absent — credit is approved but no money has moved, and that is resolved by capturing.
TABBY_STATUS_MAP = {
	"closed": "Paid",
	"rejected": "Not Paid",
	"expired": "Expired",
	"created": "Pending",
}

TABBY_AUTHORISED_STATUS = "authorized"
TABBY_CAPTURED_STATUS = "closed"
TABBY_REJECTED_STATUS = "rejected"

# `dict(frappe.request.headers)` loses werkzeug's case-insensitivity, so this has to match the canonical
# title-case werkzeug hands over.
TABBY_SIGNATURE_HEADER = "X-Webhook-Signature"

# A BNPL refusal is routine, not an error: the shopper simply did not qualify for this order. Each reason
# gets a message they can act on, because "payment failed" sends them away instead of to another method.
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

	def get_headers(self) -> dict:
		return {
			"Authorization": f"Bearer {self.get_password('key_secret')}",
			"Content-Type": "application/json",
			"X-Merchant-Code": self.merchant_code,
		}

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> dict:
		currency = currency or self.currency
		validate_transaction_currency(currency)
		validate_tabby_currency(currency)

		merchant_urls = {
			"success": self.build_return_url(self.success_url, _("Success URL"), reference),
			"cancel": self.build_return_url(self.cancelled_url, _("Cancelled URL"), reference),
			"failure": self.build_return_url(self.failure_url, _("Failure URL"), reference),
		}
		payload = {
			"payment": {
				"amount": format_tabby_amount(amount, currency),
				"currency": currency.upper(),
				# Tabby rejects a key it cannot read, so a guest who left a field empty would otherwise
				# make checkout impossible.
				"buyer": get_buyer_details(customer or {}),
				"shipping_address": get_shipping_address(customer or {}),
				"order": {"reference_id": reference},
			},
			"lang": frappe.local.lang,
			"merchant_code": self.merchant_code,
			"merchant_urls": merchant_urls,
		}

		checkout = self.post("/api/v2/checkout", payload)
		redirect_url = get_installments_url(checkout)

		# The *payment* id, not the top-level checkout id: `GET /payments/{id}`, the capture and refund
		# endpoints and the webhook body all key on the payment, and `webhook.handle` matches this value
		# against `order_ref` byte-for-byte. The checkout id is the obvious wrong choice and is unusable
		# everywhere else.
		session_id = ((checkout.get("payment") or {}).get("id")) or ""
		if not session_id:
			frappe.throw(_("Tabby returned a checkout without a payment id"))

		return {
			"session_id": session_id,
			"redirect_url": redirect_url,
			"success_url": merchant_urls["success"],
			"cancel_url": merchant_urls["cancel"],
			"failure_url": merchant_urls["failure"],
		}

	def build_return_url(self, url: str, label: str, reference: str | None) -> str:
		"""Tabby only appends `?payment_id=...` on return, so the shopper carries our request name back."""
		if not url:
			frappe.throw(_("Please set the {0} in Tabby Gateway Settings").format(label))
		# String-concatenating "?reference_id=..." breaks any URL that already carries a query.
		parts = urlsplit(get_localised_url(url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_payment(self, session_id: str) -> dict:
		return self.get_resource(f"/api/v2/payments/{session_id}")

	def get_payment_status(self, session_id: str) -> str:
		payment = self.get_payment(session_id)
		status = read_status(payment)

		if status == TABBY_AUTHORISED_STATUS:
			payment = self.capture_payment(session_id, payment)
			status = read_status(payment)

		return TABBY_STATUS_MAP.get(status, "Pending")

	def capture_payment(self, session_id: str, payment: dict) -> dict:
		"""Take the money Tabby has only authorised, and return the payment as it stands afterwards.

		This is where the shopper is actually charged: an authorised Tabby payment is approved credit and
		nothing more, so without this call the order is "approved" forever and no money ever arrives.
		With it, Paid means money taken. A failed capture raises, so the request stays Pending rather
		than reporting a charge that never happened.
		"""
		# The authorisation's own amount, never an argument: a capture can then never exceed what Tabby
		# approved, whatever the caller believes the order is worth.
		currency = payment.get("currency") or self.currency
		amount = format_tabby_amount(payment.get("amount"), currency)
		self.post(f"/api/v2/payments/{session_id}/captures", {"amount": amount})
		return self.get_payment(session_id)

	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		self.validate_webhook_source_ip()

		webhook_secret = self.get_password("webhook_secret")
		if not webhook_secret:
			frappe.throw(_("Webhook secret is not configured in Tabby Gateway Settings"))

		# Tabby does not sign its webhooks. This header is a static token we chose at registration and
		# that Tabby echoes back verbatim; it says nothing about the body. Comparing with `!=` would leak
		# a timing oracle on the one secret standing between a stranger and a Paid order.
		signature = headers.get(TABBY_SIGNATURE_HEADER)
		if not signature:
			frappe.throw(_("Missing {0} header").format(TABBY_SIGNATURE_HEADER))
		# compare_digest raises TypeError on a non-ASCII str, which would surface as an unexplained
		# TypeError instead of the mismatch this actually is.
		if not signature.isascii() or not hmac.compare_digest(webhook_secret, signature):
			frappe.throw(_("Tabby webhook token does not match"))

		# parse_json passes bytes straight through untouched, so the body is decoded first.
		# UnicodeDecodeError is a ValueError, and so is the JSONDecodeError parse_json raises on a body it
		# cannot read. Either way the delivery is not a payment we can act on.
		try:
			payment = frappe.parse_json(payload.decode())
		except ValueError:
			return {}
		if not isinstance(payment, dict):
			return {}

		session_id = payment.get("id")
		if not session_id:
			frappe.throw(_("Tabby webhook carried no payment id"))

		# ponytail: an `authorized` delivery is left Pending because capturing moves money and this path
		# runs as Guest, so a shopper who authorises and never returns to the site is not charged until a
		# status sync runs; add a scheduled sweep over Pending requests if abandoned returns show up.
		return {
			"session_id": session_id,
			"status": TABBY_STATUS_MAP.get(read_status(payment), "Pending"),
			"event_id": get_webhook_event_id(payment),
		}

	def validate_webhook_source_ip(self):
		"""Defence in depth: the shared token is the only real check, so narrow who may even present it."""
		allowed_ips = [line.strip() for line in (self.webhook_ips or "").splitlines() if line.strip()]
		if not allowed_ips:
			return
		if frappe.local.request_ip not in allowed_ips:
			frappe.throw(_("Tabby webhook came from an address that is not allowed"))

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> dict:
		currency = currency or self.currency
		payment = self.get_payment(session_id)
		if read_status(payment) != TABBY_CAPTURED_STATUS:
			# ponytail: only a captured payment is refundable — an authorised one has to be voided, and
			# Tabby's void endpoint is not wired up here; add it if orders start being cancelled between
			# authorisation and capture.
			frappe.throw(
				_(
					"Tabby has not captured this payment yet, so it cannot be refunded. An authorised but"
					" uncaptured payment has to be voided from the Tabby dashboard."
				)
			)

		# Tabby answers a refund with the updated payment, not with a refund object, so the refund it just
		# created is the last entry on the payment's ledger.
		updated_payment = self.post(
			f"/api/v2/payments/{session_id}/refunds", {"amount": format_tabby_amount(amount, currency)}
		)
		refunds = updated_payment.get("refunds") or []
		if not refunds:
			frappe.throw(_("Tabby accepted the refund but returned no refund record"))

		# Tabby reports no per-refund status; a refund it accepted and echoed back has succeeded.
		return {"refund_id": refunds[-1].get("id"), "status": "succeeded", "amount": flt(amount)}

	def post(self, endpoint: str, payload: dict) -> dict:
		url = f"{TABBY_BASE_URL}{endpoint}"
		try:
			response = make_post_request(url, json=payload, headers=self.get_headers())
		except Exception as exception:
			self.throw_request_error(url, exception)
			# throw_request_error always raises today; the bare raise keeps `response` from ever being
			# read unbound if that stops being true.
			raise

		return self.read_response(url, response)

	def get_resource(self, endpoint: str) -> dict:
		url = f"{TABBY_BASE_URL}{endpoint}"
		try:
			response = make_get_request(url, headers=self.get_headers())
		except Exception as exception:
			self.throw_request_error(url, exception)
			# throw_request_error always raises today; the bare raise keeps `response` from ever being
			# read unbound if that stops being true.
			raise

		return self.read_response(url, response)

	def throw_request_error(self, url: str, exception: Exception):
		"""Tabby explains a non-2xx in a JSON error body, so the status code alone never names it."""
		description = read_tabby_error(exception)
		self.log_request(url, error=description or exception)
		frappe.throw(_("Tabby rejected the request: {0}").format(description or _("unknown error")))

	def read_response(self, url: str, response) -> dict:
		if not isinstance(response, dict):
			self.log_request(url, error="Tabby returned a non-JSON body")
			frappe.throw(_("Tabby returned an unreadable response"))

		if error := response.get("errorType"):
			self.log_request(url, error=response.get("error") or error)
			frappe.throw(_("Tabby rejected the request: {0}").format(response.get("error") or error))

		self.log_request(url, output=summarise_tabby_payment(response))
		return response

	def log_request(self, url: str, output=None, error=None):
		# The headers carry the Bearer secret and the payload carries the shopper's name, phone and
		# address, so only the endpoint and the outcome are logged.
		create_request_log(
			{"endpoint": url},
			service_name="Tabby",
			is_remote_request=True,
			reference_doctype=self.doctype,
			reference_docname=self.name,
			output=output,
			error=error,
			status="Failed" if error else "Completed",
		)


def validate_tabby_currency(currency: str):
	if (currency or "").strip().upper() not in TABBY_SUPPORTED_CURRENCIES:
		frappe.throw(
			_("Tabby cannot take payment in {0}. It supports {1}.").format(
				frappe.bold(currency), ", ".join(TABBY_SUPPORTED_CURRENCIES)
			)
		)


def format_tabby_amount(amount, currency: str) -> str:
	"""Tabby bills in major units as a decimal string, at the currency's own precision.

	KWD and BHD carry three decimals, so a plain `str(float)` under-specifies them and Tabby reads a
	different figure than the one the shopper agreed to.
	"""
	exponent = get_minor_unit_exponent(currency)
	return f"{flt(amount, exponent):.{exponent}f}"


def read_status(payment: dict) -> str:
	"""Tabby's status is lowercase, but normalising once keeps a stray " Closed " from slipping past."""
	return ((payment or {}).get("status") or "").strip().casefold()


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
	# ponytail: Tabby also reads a `zip`, but the shared customer contract carries country rather than a
	# postcode, so it goes unsent; add it to `GatewayPaymentRequest.get_customer_details` if Tabby's
	# approval rate ever turns out to depend on it.
	address = customer.get("address") or {}
	details = {"address": address.get("line1"), "city": address.get("city")}
	return {key: value for key, value in details.items() if value}


def get_installments_url(checkout: dict) -> str:
	"""Return the hosted checkout URL, or explain the refusal in words a shopper can act on."""
	configuration = checkout.get("configuration") or {}
	# `available_products` holds a *list* of installment plans, while `products` holds an object carrying
	# the rejection reason. Indexing the list unguarded shows a declined shopper a raw traceback.
	installments = ((configuration.get("available_products") or {}).get("installments")) or []
	if read_status(checkout) == TABBY_REJECTED_STATUS or not installments:
		throw_rejection(configuration)

	web_url = (installments[0] or {}).get("web_url")
	if not web_url:
		throw_rejection(configuration)
	return web_url


def throw_rejection(configuration: dict):
	products = configuration.get("products") or {}
	reason = ((products.get("installments") or {}).get("rejection_reason") or "").strip().casefold()
	frappe.throw(_(TABBY_REJECTION_MESSAGES.get(reason, DEFAULT_REJECTION_MESSAGE)), title=_("Tabby"))


def summarise_tabby_payment(response: dict) -> dict:
	"""Tabby echoes the shopper's name, phone and address, so only the ids are logged."""
	# A checkout nests the payment; every other endpoint answers with the payment itself.
	payment = response.get("payment") or response
	return {"id": payment.get("id"), "status": response.get("status")}


def read_tabby_error(exception: Exception) -> str | None:
	response = getattr(exception, "response", None)
	if response is None:
		return None
	try:
		body = response.json()
	except ValueError:
		return None
	return (body or {}).get("error") or (body or {}).get("errorType")


def get_webhook_event_id(payment: dict) -> str:
	"""Tabby sends no event id, so replay protection needs a stable one derived from the delivery itself.

	Identical redeliveries of the same payment state then collapse to one applied event.
	"""
	# ponytail: a digest over the body is weaker than a gateway-issued id — two genuinely separate
	# transitions that carry the same id, status and timestamp would dedupe into one; switch to Tabby's
	# own event id the day they start sending one.
	timestamp = payment.get("updated_at") or payment.get("created_at") or ""
	fingerprint = f"{payment.get('id')}|{payment.get('status')}|{timestamp}"
	return hashlib.sha256(fingerprint.encode()).hexdigest()
