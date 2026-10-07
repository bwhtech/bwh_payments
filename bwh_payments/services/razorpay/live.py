import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request

from bwh_payments.services.razorpay.base import (
	BaseRazorpayClient,
	RazorpayPaymentLink,
	RazorpayRefund,
)

# ponytail: frappe.integrations.utils.make_request takes no timeout, so a hung Razorpay call holds a
# worker; revisit if Razorpay latency ever shows up in the request log.
RAZORPAY_BASE_URL = "https://api.razorpay.com/v1"
RAZORPAY_HEADERS = {"accept": "application/json", "Content-Type": "application/json"}
SETTINGS_DOCTYPE = "Razorpay Gateway Settings"


class LiveRazorpayClient(BaseRazorpayClient):
	def __init__(self, key_id: str, key_secret: str):
		self.auth = (key_id, key_secret)

	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.post("/payment_links", payload))

	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.get(f"/payment_links/{link_id}"))

	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink:
		return RazorpayPaymentLink.model_validate(self.post(f"/payment_links/{link_id}/cancel", {}))

	def create_refund(self, payment_id: str, amount: int) -> RazorpayRefund:
		return RazorpayRefund.model_validate(self.post(f"/payments/{payment_id}/refund", {"amount": amount}))

	def post(self, endpoint: str, payload: dict) -> dict:
		url = f"{RAZORPAY_BASE_URL}{endpoint}"
		try:
			response = make_post_request(url, auth=self.auth, json=payload, headers=RAZORPAY_HEADERS)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)

	def get(self, endpoint: str) -> dict:
		url = f"{RAZORPAY_BASE_URL}{endpoint}"
		try:
			response = make_get_request(url, auth=self.auth, headers=RAZORPAY_HEADERS)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)


def throw_request_error(url: str, exception: Exception):
	"""Razorpay explains a non-2xx in a JSON error body, so the status code alone never names it."""
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
	# The request payload carries the API key and the shopper's contact details, so only the endpoint and
	# the outcome are logged.
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
	"""Razorpay echoes the shopper's name, email and phone number, so only the ids are logged."""
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
