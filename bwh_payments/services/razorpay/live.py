from typing import NoReturn

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from requests import RequestException

from bwh_payments.services import GatewayRefusedError, GatewayUnavailableError
from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayOrder,
	RazorpayPayment,
	RazorpayPaymentDetails,
	RazorpayPaymentLink,
	RazorpayRefund,
)

RAZORPAY_BASE_URL = "https://api.razorpay.com/v1"
RAZORPAY_HEADERS = {"accept": "application/json", "Content-Type": "application/json"}
SETTINGS_DOCTYPE = "Razorpay Gateway Settings"
TIMEOUT_SECONDS = 30
REFUND_IDEMPOTENCY_HEADER = "X-Refund-Idempotency"
# 409: a request with the same idempotency key is still in progress.
TRANSIENT_STATUS_CODES = (409, 429)


class LiveRazorpayClient(BaseRazorpayClient):
	def __init__(self, key_id: str, key_secret: str):
		self.auth = (key_id, key_secret)

	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.post("/payment_links", payload))

	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.get(f"/payment_links/{link_id}"))

	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.post(f"/payment_links/{link_id}/cancel", {}))

	def create_order(self, payload: dict) -> RazorpayOrder:
		return RazorpayOrder.model_validate(self.post("/orders", payload))

	def get_order(self, order_id: str) -> RazorpayOrder:
		return RazorpayOrder.model_validate(self.get(f"/orders/{order_id}"))

	def get_order_payments(self, order_id: str) -> list[RazorpayPayment]:
		items = self.get(f"/orders/{order_id}/payments").get("items") or []
		return [RazorpayPayment(payment_id=item["id"], status=item.get("status")) for item in items]

	def get_payment(self, payment_id: str) -> RazorpayPaymentDetails:
		return RazorpayPaymentDetails.model_validate(self.get(f"/payments/{payment_id}"))

	def create_refund(
		self, payment_id: str, amount: int, idempotency_key: str | None = None
	) -> RazorpayRefund:
		headers = {REFUND_IDEMPOTENCY_HEADER: idempotency_key} if idempotency_key else {}
		return RazorpayRefund.model_validate(
			self.post(f"/payments/{payment_id}/refund", {"amount": amount}, headers)
		)

	def get_refund(self, refund_id: str) -> RazorpayRefund:
		return RazorpayRefund.model_validate(self.get(f"/refunds/{refund_id}"))

	def post(self, endpoint: str, payload: dict, headers: dict | None = None) -> dict:
		url = f"{RAZORPAY_BASE_URL}{endpoint}"
		try:
			response = make_post_request(
				url,
				auth=self.auth,
				json=payload,
				headers={**RAZORPAY_HEADERS, **(headers or {})},
				timeout=TIMEOUT_SECONDS,
			)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)

	def get(self, endpoint: str) -> dict:
		url = f"{RAZORPAY_BASE_URL}{endpoint}"
		try:
			response = make_get_request(
				url, auth=self.auth, headers=RAZORPAY_HEADERS, timeout=TIMEOUT_SECONDS
			)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)


def throw_request_error(url: str, exception: Exception) -> NoReturn:
	description = read_razorpay_error(exception)
	log_request(url, error=description or exception)
	frappe.throw(
		_("Razorpay rejected the request: {0}").format(description or _("unknown error")),
		exc=get_error_class(exception),
	)


def get_error_class(exception: Exception) -> type[frappe.ValidationError]:
	"""Only a 4xx is Razorpay's own no. No answer, a server error, a rate limit, or the same refund still
	in progress is unavailable: a retry may work."""
	response = getattr(exception, "response", None)
	if response is None:
		return GatewayUnavailableError if isinstance(exception, RequestException) else frappe.ValidationError
	if response.status_code >= 500 or response.status_code in TRANSIENT_STATUS_CODES:
		return GatewayUnavailableError
	return GatewayRefusedError


def read_response(url: str, response) -> dict:
	if not isinstance(response, dict):
		log_request(url, error="Razorpay returned a non-JSON body")
		frappe.throw(_("Razorpay returned an unreadable response"))

	if error := response.get("error"):
		log_request(url, error=error.get("description") or error.get("code"))
		frappe.throw(
			_("Razorpay rejected the request: {0}").format(error.get("description")), GatewayRefusedError
		)

	log_request(url, output=summarise_razorpay_entity(response))
	return response


def log_request(url: str, output=None, error=None):
	# The payload carries the API key and shopper details, so only the endpoint is logged.
	create_request_log(
		{"endpoint": url},
		service_name="Razorpay",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)


def summarise_razorpay_entity(response: dict) -> dict:
	return {"id": response.get("id"), "status": response.get("status")}


def read_razorpay_error(exception: Exception) -> str | None:
	response = getattr(exception, "response", None)
	if response is None:
		return None
	try:
		body = response.json()
	except ValueError:
		return None
	return ((body or {}).get("error") or {}).get("description")
