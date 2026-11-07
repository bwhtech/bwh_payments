from typing import NoReturn

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_request

from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayEntity,
	RazorpayPaymentDetails,
	RazorpayPaymentLink,
	RazorpayProduct,
	RazorpayRefund,
	RazorpayTransfer,
)

RAZORPAY_BASE_URL = "https://api.razorpay.com"
RAZORPAY_HEADERS = {"accept": "application/json", "Content-Type": "application/json"}
SETTINGS_DOCTYPE = "Razorpay Gateway Settings"


class LiveRazorpayClient(BaseRazorpayClient):
	def __init__(self, key_id: str, key_secret: str):
		self.auth = (key_id, key_secret)

	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.request("POST", "/v1/payment_links", payload))

	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.request("GET", f"/v1/payment_links/{link_id}"))

	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(
			self.request("POST", f"/v1/payment_links/{link_id}/cancel", {})
		)

	def create_refund(self, payment_id: str, amount: int) -> RazorpayRefund:
		return RazorpayRefund.model_validate(
			self.request("POST", f"/v1/payments/{payment_id}/refund", {"amount": amount})
		)

	def get_refund(self, refund_id: str) -> RazorpayRefund:
		return RazorpayRefund.model_validate(self.request("GET", f"/v1/refunds/{refund_id}"))

	def get_payment(self, payment_id: str) -> RazorpayPaymentDetails:
		return RazorpayPaymentDetails.model_validate(self.request("GET", f"/v1/payments/{payment_id}"))

	def get_payment_transfers(self, payment_id: str) -> list[RazorpayTransfer]:
		response = self.request("GET", f"/v1/payments/{payment_id}/transfers")
		return [RazorpayTransfer.model_validate(item) for item in response.get("items") or []]

	def reverse_transfer(self, transfer_id: str, amount: int) -> None:
		self.request("POST", f"/v1/transfers/{transfer_id}/reversals", {"amount": amount})

	def update_transfer(self, transfer_id: str, payload: dict) -> RazorpayTransfer:
		return RazorpayTransfer.model_validate(self.request("PATCH", f"/v1/transfers/{transfer_id}", payload))

	def create_account(self, payload: dict) -> RazorpayEntity:
		return RazorpayEntity.model_validate(self.request("POST", "/v2/accounts", payload))

	def update_account(self, account_id: str, payload: dict) -> RazorpayEntity:
		return RazorpayEntity.model_validate(self.request("PATCH", f"/v2/accounts/{account_id}", payload))

	def create_stakeholder(self, account_id: str, payload: dict) -> RazorpayEntity:
		return RazorpayEntity.model_validate(
			self.request("POST", f"/v2/accounts/{account_id}/stakeholders", payload)
		)

	def update_stakeholder(self, account_id: str, stakeholder_id: str, payload: dict) -> RazorpayEntity:
		return RazorpayEntity.model_validate(
			self.request("PATCH", f"/v2/accounts/{account_id}/stakeholders/{stakeholder_id}", payload)
		)

	def request_product(self, account_id: str, payload: dict) -> RazorpayProduct:
		return RazorpayProduct.model_validate(
			self.request("POST", f"/v2/accounts/{account_id}/products", payload)
		)

	def update_product(self, account_id: str, product_id: str, payload: dict) -> RazorpayProduct:
		return RazorpayProduct.model_validate(
			self.request("PATCH", f"/v2/accounts/{account_id}/products/{product_id}", payload)
		)

	def get_product(self, account_id: str, product_id: str) -> RazorpayProduct:
		return RazorpayProduct.model_validate(
			self.request("GET", f"/v2/accounts/{account_id}/products/{product_id}")
		)

	def request(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
		url = f"{RAZORPAY_BASE_URL}{endpoint}"
		try:
			response = make_request(method, url, auth=self.auth, json=payload, headers=RAZORPAY_HEADERS)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)


def throw_request_error(url: str, exception: Exception) -> NoReturn:
	description = read_razorpay_error(exception)
	log_request(url, error=description or exception)
	frappe.throw(_("Razorpay rejected the request: {0}").format(description or _("unknown error")))


def read_response(url: str, response) -> dict:
	if not isinstance(response, dict):
		log_request(url, error="Razorpay returned a non-JSON body")
		frappe.throw(_("Razorpay returned an unreadable response"))

	if error := response.get("error"):
		log_request(url, error=error.get("description") or error.get("code"))
		frappe.throw(_("Razorpay rejected the request: {0}").format(error.get("description")))

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
