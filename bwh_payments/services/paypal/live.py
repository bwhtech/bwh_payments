import base64
from typing import NoReturn

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.utils.data import cint
from requests import RequestException

from bwh_payments.services import GatewayUnavailableError
from bwh_payments.services.paypal.base import (
	BasePayPalClient,
	PayPalOrder,
	PayPalOrderAlreadyCapturedError,
	PayPalRefund,
	PayPalWebhookVerification,
)

PAYPAL_BASE_URLS = {
	"Sandbox": "https://api-m.sandbox.paypal.com",
	"Live": "https://api-m.paypal.com",
}
PAYPAL_ALREADY_CAPTURED_ISSUE = "ORDER_ALREADY_CAPTURED"
SETTINGS_DOCTYPE = "PayPal Gateway Settings"


class LivePayPalClient(BasePayPalClient):
	def __init__(self, mode: str, client_id: str, client_secret: str):
		self.mode = mode or "Sandbox"
		self.base_url = PAYPAL_BASE_URLS[self.mode]
		self.client_id = client_id
		self.client_secret = client_secret

	def create_order(self, payload: dict, request_id: str | None = None) -> PayPalOrder:
		return PayPalOrder.model_validate(self.post("/v2/checkout/orders", payload, request_id))

	def get_order(self, order_id: str) -> PayPalOrder:
		return PayPalOrder.model_validate(self.get(f"/v2/checkout/orders/{order_id}"))

	def capture_order(self, order_id: str) -> PayPalOrder:
		# The order id as request id: PayPal replays the first outcome instead of capturing twice.
		return PayPalOrder.model_validate(
			self.post(f"/v2/checkout/orders/{order_id}/capture", {}, request_id=order_id)
		)

	def refund_capture(self, capture_id: str, payload: dict) -> PayPalRefund:
		# No request id: a stable one would replay the first refund instead of taking a second partial.
		return PayPalRefund.model_validate(self.post(f"/v2/payments/captures/{capture_id}/refund", payload))

	def get_refund(self, refund_id: str) -> PayPalRefund:
		return PayPalRefund.model_validate(self.get(f"/v2/payments/refunds/{refund_id}"))

	def verify_webhook_signature(self, payload: dict) -> PayPalWebhookVerification:
		return PayPalWebhookVerification.model_validate(
			self.post("/v1/notifications/verify-webhook-signature", payload)
		)

	def get_access_token(self) -> str:
		# `frappe.cache` is per site. The mode and client id keep a token to the account that issued it,
		# so new credentials never create orders in the old account.
		cache_key = f"paypal_access_token::{self.mode}::{self.client_id}"
		if token := frappe.cache.get_value(cache_key):
			return token

		url = f"{self.base_url}/v1/oauth2/token"
		credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
		headers = {
			"Authorization": f"Basic {credentials}",
			"Content-Type": "application/x-www-form-urlencoded",
		}
		try:
			response = make_post_request(url, headers=headers, data="grant_type=client_credentials")
		except Exception as exception:
			throw_request_error(url, exception)

		token = response.get("access_token") if isinstance(response, dict) else None
		if not token:
			log_request(url, error="PayPal returned no access token")
			frappe.throw(_("PayPal did not return an access token"))

		# Expire early: a token lapsing mid-checkout surfaces as an unexplained 401.
		ttl = max(cint(response.get("expires_in")) - 300, 60)
		frappe.cache.set_value(cache_key, token, expires_in_sec=ttl)
		return token

	def get_headers(self, request_id: str | None = None) -> dict:
		headers = {
			"Authorization": f"Bearer {self.get_access_token()}",
			"Content-Type": "application/json",
		}
		if request_id:
			headers["PayPal-Request-Id"] = request_id
		return headers

	def post(self, endpoint: str, payload: dict, request_id: str | None = None) -> dict:
		url = f"{self.base_url}{endpoint}"
		try:
			response = make_post_request(url, json=payload, headers=self.get_headers(request_id))
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)

	def get(self, endpoint: str) -> dict:
		url = f"{self.base_url}{endpoint}"
		try:
			response = make_get_request(url, headers=self.get_headers())
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)


def throw_request_error(url: str, exception: Exception) -> NoReturn:
	body = read_paypal_error_body(exception) or {}
	detail = (body.get("details") or [{}])[0] or {}
	description = detail.get("description") or body.get("message") or body.get("name")
	log_request(url, error=description or exception)
	issue = (detail.get("issue") or "").strip().upper()
	if issue == PAYPAL_ALREADY_CAPTURED_ISSUE:
		exc = PayPalOrderAlreadyCapturedError
	elif is_transient_error(exception):
		exc = GatewayUnavailableError
	else:
		exc = frappe.ValidationError
	frappe.throw(_("PayPal rejected the request: {0}").format(description or _("unknown error")), exc=exc)


def read_response(url: str, response) -> dict:
	if not isinstance(response, dict):
		log_request(url, error="PayPal returned a non-JSON body")
		frappe.throw(_("PayPal returned an unreadable response"))

	log_request(url, output={"id": response.get("id"), "status": response.get("status")})
	return response


def log_request(url: str, output=None, error=None):
	# Headers carry the bearer token and payloads the shopper's details, so only the endpoint is logged.
	create_request_log(
		{"endpoint": url},
		service_name="PayPal",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)


def is_transient_error(exception: Exception) -> bool:
	response = getattr(exception, "response", None)
	if response is None:
		return isinstance(exception, RequestException)
	return response.status_code >= 500 or response.status_code == 429


def read_paypal_error_body(exception: Exception) -> dict | None:
	response = getattr(exception, "response", None)
	if response is None:
		return None
	try:
		body = response.json()
	except ValueError:
		return None
	return body if isinstance(body, dict) else None
