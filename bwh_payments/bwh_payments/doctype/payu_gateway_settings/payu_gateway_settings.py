# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import hmac
import json
from html import escape
from urllib.parse import parse_qsl

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.rate_limiter import rate_limit
from frappe.utils import get_url
from frappe.utils.data import flt
from werkzeug.wrappers import Response

from bwh_payments.base_class import CheckoutSession, PaymentGatewayBase, RefundResult, WebhookEvent
from bwh_payments.bwh_payments.utils import add_reference_id
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency
from bwh_payments.services.payu.base import BasePayUClient, payu_hash
from bwh_payments.services.payu.live import LivePayUClient
from bwh_payments.services.payu.stub import StubPayUClient

SETTINGS_DOCTYPE = "PayU Gateway Settings"
ENDPOINT = "/api/method/bwh_payments.bwh_payments.doctype.payu_gateway_settings.payu_gateway_settings"
PAYU_TEST_CHECKOUT_URL = "https://test.payu.in/_payment"
PAYU_LIVE_CHECKOUT_URL = "https://secure.payu.in/_payment"
PAYU_SUPPORTED_CURRENCIES = ("INR",)
PAYU_CAPTURED_STATUS = "captured"

# Keyed on `unmappedstatus`. Unmapped states (auth, pending, in progress, not found) stay Pending.
PAYU_STATUS_MAP = {
	"captured": "Paid",
	"usercancelled": "Cancelled",
	"failed": "Not Paid",
	"bounced": "Not Paid",
	"dropped": "Not Paid",
	"autorefund": "Not Paid",
}
# To `gateway_refund.REFUND_STATUS_MAP` values; anything else is pending.
PAYU_REFUND_STATUS_MAP = {"success": "processed", "failure": "failed", "failed": "failed"}

CHECKOUT_FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body onload="document.forms[0].submit()">
<form method="post" action="{action}">{inputs}<button type="submit">{button}</button></form>
</body></html>"""


class PayUGatewaySettings(Document, PaymentGatewayBase):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cancelled_url: DF.Data | None
		enabled: DF.Check
		failure_url: DF.Data | None
		merchant_key: DF.Data
		merchant_salt: DF.Password
		success_url: DF.Data | None
		test_mode: DF.Check
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "PayU"

	def get_client(self) -> BasePayUClient:
		if frappe.in_test:
			return StubPayUClient()

		return LivePayUClient(self.merchant_key, self.get_password("merchant_salt"), bool(self.test_mode))

	def supported_currencies(self) -> tuple[str, ...]:
		return PAYU_SUPPORTED_CURRENCIES

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
	) -> CheckoutSession:
		"""PayU checkout is a form the browser posts, so no PayU call is made until `start_checkout`."""
		validate_transaction_currency(currency)
		if currency.strip().upper() not in PAYU_SUPPORTED_CURRENCIES:
			frappe.throw(_("PayU takes payments only in INR"))
		# PayU's page dead-ends without them, so refuse here rather than strand the shopper.
		customer = customer or {}
		if not (customer.get("email") and customer.get("phone")):
			frappe.throw(_("PayU needs the shopper's email and phone"))
		if not (return_url or (self.success_url and self.failure_url)):
			frappe.throw(_("Please set the Success URL and Failure URL in PayU Gateway Settings"))

		return CheckoutSession(
			session_id=frappe.generate_hash(length=20),
			redirect_url=get_url(f"{ENDPOINT}.start_checkout?request={reference}"),
			success_url=return_url or add_reference_id(self.success_url, reference),
			failure_url=return_url or add_reference_id(self.failure_url, reference),
			cancel_url=return_url or (self.cancelled_url and add_reference_id(self.cancelled_url, reference)),
		)

	def build_checkout_form(self, payment_request) -> tuple[str, dict]:
		"""The action URL and fields of the form that sends the shopper to PayU."""
		fields = {
			"key": self.merchant_key,
			"txnid": payment_request.order_ref,
			"amount": format_payu_amount(payment_request.amount, payment_request.currency_code),
			"productinfo": payment_request.name,
			"firstname": payment_request.customer_forenames or "Customer",
			"email": payment_request.customer_email or "",
			"phone": payment_request.customer_phone or "",
		}
		# sha512(key|txnid|amount|productinfo|firstname|email|udf1..udf5||||||salt), udfs left empty.
		fields["hash"] = payu_hash(
			*(fields[name] for name in ("key", "txnid", "amount", "productinfo", "firstname", "email")),
			*[""] * 10,
			self.get_password("merchant_salt"),
		)
		bounce = f"{ENDPOINT}.return_from_payu?request={payment_request.name}&outcome="
		fields["surl"] = get_url(f"{bounce}success")
		fields["furl"] = get_url(f"{bounce}failure")
		return (PAYU_TEST_CHECKOUT_URL if self.test_mode else PAYU_LIVE_CHECKOUT_URL), fields

	def get_payment_status(self, session_id: str) -> str:
		transaction = self.get_client().verify_payment(session_id)
		return PAYU_STATUS_MAP.get(read_status(transaction.unmappedstatus), "Pending")

	def get_transaction_ref(self, session_id: str) -> str | None:
		transaction = self.get_client().verify_payment(session_id)
		if read_status(transaction.unmappedstatus) == PAYU_CAPTURED_STATUS:
			return transaction.mihpayid
		return None

	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		data = read_body(payload)
		if data.get("request_id") and not data.get("hash"):
			return self.read_refund_event(data["request_id"])
		if not (data.get("hash") or data.get("txnid")):
			return None

		txnid = data.get("txnid")
		signature = data.get("hash") or ""
		if not (txnid and signature):
			frappe.throw(_("PayU payment event without a txnid or hash"))

		# compare_digest raises TypeError on a non-ASCII str.
		if not signature.isascii() or not hmac.compare_digest(self.get_response_hash(data), signature):
			frappe.throw(_("PayU webhook hash does not match"))

		unmappedstatus = read_status(data.get("unmappedstatus"))
		return WebhookEvent(
			session_id=txnid,
			status=PAYU_STATUS_MAP.get(unmappedstatus, "Pending"),
			event_id=f"{txnid}:{data.get('mihpayid')}:{unmappedstatus}",
		)

	def get_response_hash(self, data: dict) -> str:
		"""sha512([additionalCharges|]salt|status||||||udf5..udf1|email|firstname|productinfo|amount|txnid|key).

		Our own key, not the posted one, so an event for another merchant fails the check.
		"""
		parts = [
			self.get_password("merchant_salt"),
			data.get("status") or "",
			*[""] * 5,
			*(data.get(f"udf{index}") or "" for index in range(5, 0, -1)),
			*(data.get(name) or "" for name in ("email", "firstname", "productinfo", "amount", "txnid")),
			self.merchant_key,
		]
		if data.get("additionalCharges"):
			parts.insert(0, data["additionalCharges"])
		return payu_hash(*parts)

	def read_refund_event(self, request_id: str) -> WebhookEvent:
		# PayU does not sign refund webhooks, so the body only names the refund; PayU is asked for the rest.
		refund = self.get_client().get_refund(request_id)
		status = read_refund_status(refund.status)
		return WebhookEvent(
			refund_id=request_id,
			status=status,
			refund_amount=flt(refund.amt),
			transaction_ref=refund.mihpayid,
			event_id=f"{request_id}:{status}",
		)

	def get_refund_status(self, session_id: str, refund_id: str) -> RefundResult:
		refund = self.get_client().get_refund(refund_id)
		return RefundResult(refund_id=refund_id, status=read_refund_status(refund.status))

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		client = self.get_client()
		transaction = client.verify_payment(session_id)
		if read_status(transaction.unmappedstatus) != PAYU_CAPTURED_STATUS:
			frappe.throw(_("PayU has no captured payment to refund"))

		# PayU dedupes refunds on the token, at most 23 characters.
		refund = client.refund(
			transaction.mihpayid,
			frappe.generate_hash(length=20),
			format_payu_amount(amount, currency or "INR"),
		)
		# Accepted means queued; the refund webhook or a sync settles it.
		return RefundResult(refund_id=refund.request_id, status="pending", amount=amount)


def read_status(status: str | None) -> str:
	return (status or "").strip().casefold()


def read_refund_status(status: str | None) -> str:
	return PAYU_REFUND_STATUS_MAP.get(read_status(status), "pending")


def read_body(payload: bytes) -> dict:
	text = payload.decode()
	if text.lstrip().startswith("{"):
		data = json.loads(text)
		return data if isinstance(data, dict) else {}
	return dict(parse_qsl(text, keep_blank_values=True))


def format_payu_amount(amount: float, currency: str) -> str:
	exponent = get_minor_unit_exponent(currency)
	return f"{flt(amount, exponent):.{exponent}f}"


def get_payu_request(request: str):
	"""The request behind a guest link, refused as not found unless it is a PayU checkout."""
	gateway = frappe.db.get_value("Gateway Payment Request", request, "gateway")
	if not gateway or frappe.db.get_value("Payment Gateway Profile", gateway, "gateway_settings") != (
		SETTINGS_DOCTYPE
	):
		raise frappe.DoesNotExistError(_("Payment not found"))
	return frappe.get_doc("Gateway Payment Request", request)


# nosemgrep: guest-whitelisted-method  # the shopper's browser opens it; it only renders the stored checkout
@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=60, seconds=60, ip_based=True)
def start_checkout(request: str) -> Response:
	payment_request = get_payu_request(request)
	if payment_request.status != "Pending":
		raise frappe.DoesNotExistError(_("Payment not found"))

	action, fields = frappe.get_single(SETTINGS_DOCTYPE).build_checkout_form(payment_request)
	inputs = "".join(
		f'<input type="hidden" name="{escape(name)}" value="{escape(str(value))}">'
		for name, value in fields.items()
	)
	html = CHECKOUT_FORM.format(
		title=escape(_("Redirecting to PayU")),
		action=escape(action),
		inputs=inputs,
		button=escape(_("Continue to PayU")),
	)
	return Response(html, content_type="text/html; charset=utf-8")


# nosemgrep: guest-whitelisted-method  # PayU posts the shopper back here; it ignores the body and only redirects
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@rate_limit(limit=60, seconds=60, ip_based=True)
def return_from_payu(request: str, outcome: str):
	"""Send the shopper on to the stored return URL. The status is confirmed there, never from this POST."""
	payment_request = get_payu_request(request)
	location = payment_request.success_url if outcome == "success" else payment_request.failure_url
	if not location:
		raise frappe.DoesNotExistError(_("Payment not found"))

	# PayU's cross-site POST carries no Lax cookies, so Frappe saw a Guest and queued sid=Guest cookies
	# that would sign the shopper out. Dropping them keeps the shopper's own session.
	frappe.local.cookie_manager.cookies = {}
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location
