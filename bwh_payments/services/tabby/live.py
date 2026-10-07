import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request

from bwh_payments.services.tabby.base import BaseTabbyClient, TabbyCheckout, TabbyPayment

# ponytail: frappe.integrations.utils.make_request takes no timeout, so a hung Tabby call holds a worker;
# revisit if Tabby latency ever shows up in the request log.
TABBY_BASE_URL = "https://api.tabby.ai"
SETTINGS_DOCTYPE = "Tabby Gateway Settings"


class LiveTabbyClient(BaseTabbyClient):
	def __init__(self, key_secret: str, merchant_code: str):
		self.headers = {
			"Authorization": f"Bearer {key_secret}",
			"Content-Type": "application/json",
			"X-Merchant-Code": merchant_code,
		}

	def create_checkout(self, payload: dict) -> TabbyCheckout:
		return TabbyCheckout.model_validate(self.post("/api/v2/checkout", payload))

	def get_payment(self, payment_id: str) -> TabbyPayment:
		return TabbyPayment.model_validate(self.get(f"/api/v2/payments/{payment_id}"))

	def capture_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		return TabbyPayment.model_validate(
			self.post(f"/api/v2/payments/{payment_id}/captures", {"amount": amount})
		)

	def refund_payment(self, payment_id: str, amount: str) -> TabbyPayment:
		return TabbyPayment.model_validate(
			self.post(f"/api/v2/payments/{payment_id}/refunds", {"amount": amount})
		)

	def post(self, endpoint: str, payload: dict) -> dict:
		url = f"{TABBY_BASE_URL}{endpoint}"
		try:
			response = make_post_request(url, json=payload, headers=self.headers)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)

	def get(self, endpoint: str) -> dict:
		url = f"{TABBY_BASE_URL}{endpoint}"
		try:
			response = make_get_request(url, headers=self.headers)
		except Exception as exception:
			throw_request_error(url, exception)
		return read_response(url, response)


def throw_request_error(url: str, exception: Exception):
	"""Tabby explains a non-2xx in a JSON error body, so the status code alone never names it."""
	description = read_tabby_error(exception)
	log_request(url, error=description or exception)
	frappe.throw(_("Tabby rejected the request: {0}").format(description or _("unknown error")))


def read_response(url: str, response) -> dict:
	if not isinstance(response, dict):
		log_request(url, error="Tabby returned a non-JSON body")
		frappe.throw(_("Tabby returned an unreadable response"))

	if error := response.get("errorType"):
		log_request(url, error=response.get("error") or error)
		frappe.throw(_("Tabby rejected the request: {0}").format(response.get("error") or error))

	log_request(url, output=summarise_tabby_payment(response))
	return response


def log_request(url: str, output=None, error=None):
	# The headers carry the Bearer secret and the payload carries the shopper's name, phone and address,
	# so only the endpoint and the outcome are logged.
	create_request_log(
		{"endpoint": url},
		service_name="Tabby",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)


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
