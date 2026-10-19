import base64
from datetime import datetime
from typing import NoReturn
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from requests import RequestException

from bwh_payments.services import GatewayUnavailableError
from bwh_payments.services.mpesa.base import BaseMpesaClient, MpesaReversal, MpesaStkPush, MpesaStkQuery

MPESA_SANDBOX_URL = "https://sandbox.safaricom.co.ke"
MPESA_LIVE_URL = "https://api.safaricom.co.ke"
MPESA_TIMEZONE = ZoneInfo("Africa/Nairobi")
# Daraja answers an STK query with this error until the shopper has answered the prompt.
MPESA_STILL_PROCESSING_ERROR = "500.001.1001"
MPESA_ACCEPTED_RESPONSE_CODE = "0"
SETTINGS_DOCTYPE = "Mpesa Gateway Settings"


class LiveMpesaClient(BaseMpesaClient):
	def __init__(
		self,
		consumer_key: str,
		consumer_secret: str,
		shortcode: str,
		passkey: str,
		sandbox: bool,
		initiator_name: str | None = None,
		security_credential: str | None = None,
	):
		self.auth = (consumer_key, consumer_secret)
		self.shortcode = shortcode
		self.passkey = passkey
		self.base_url = MPESA_SANDBOX_URL if sandbox else MPESA_LIVE_URL
		self.initiator_name = initiator_name
		self.security_credential = security_credential
		self.access_token = None

	def send_stk_push(self, payload: dict) -> MpesaStkPush:
		return MpesaStkPush.model_validate(
			self.post("/mpesa/stkpush/v1/processrequest", {**self.get_stk_credentials(), **payload})
		)

	def query_stk_push(self, checkout_request_id: str) -> MpesaStkQuery:
		payload = {**self.get_stk_credentials(), "CheckoutRequestID": checkout_request_id}
		return MpesaStkQuery.model_validate(
			self.post("/mpesa/stkpushquery/v1/query", payload, still_processing_is_empty=True)
		)

	def reverse_transaction(self, payload: dict) -> MpesaReversal:
		if not (self.initiator_name and self.security_credential):
			frappe.throw(_("Set the Initiator Name and Security Credential in Mpesa Gateway Settings"))
		credentials = {"Initiator": self.initiator_name, "SecurityCredential": self.security_credential}
		return MpesaReversal.model_validate(
			self.post("/mpesa/reversal/v1/request", {**credentials, **payload})
		)

	def get_stk_credentials(self) -> dict:
		timestamp = datetime.now(MPESA_TIMEZONE).strftime("%Y%m%d%H%M%S")
		password = base64.b64encode(f"{self.shortcode}{self.passkey}{timestamp}".encode()).decode()
		return {"BusinessShortCode": self.shortcode, "Password": password, "Timestamp": timestamp}

	def get_access_token(self) -> str:
		if self.access_token:
			return self.access_token

		url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"
		try:
			response = make_get_request(url, auth=self.auth)
		except Exception as exception:
			throw_request_error(url, exception)
		if not isinstance(response, dict) or not response.get("access_token"):
			log_request(url, error="M-Pesa returned no access token")
			frappe.throw(_("M-Pesa did not accept the Consumer Key and Consumer Secret"))

		self.access_token = response["access_token"]
		return self.access_token

	def post(self, endpoint: str, payload: dict, still_processing_is_empty: bool = False) -> dict:
		url = f"{self.base_url}{endpoint}"
		headers = {"Authorization": f"Bearer {self.get_access_token()}", "Content-Type": "application/json"}
		try:
			response = make_post_request(url, json=payload, headers=headers)
		except Exception as exception:
			if still_processing_is_empty and read_mpesa_error_code(exception) == MPESA_STILL_PROCESSING_ERROR:
				log_request(url, output={"errorCode": MPESA_STILL_PROCESSING_ERROR})
				return {}
			throw_request_error(url, exception)
		return read_response(url, response)


def throw_request_error(url: str, exception: Exception) -> NoReturn:
	description = read_mpesa_error(exception)
	log_request(url, error=description or exception)
	exc = GatewayUnavailableError if is_transient_error(exception) else frappe.ValidationError
	frappe.throw(_("M-Pesa rejected the request: {0}").format(description or _("unknown error")), exc=exc)


def is_transient_error(exception: Exception) -> bool:
	response = getattr(exception, "response", None)
	if response is None:
		return isinstance(exception, RequestException)
	return response.status_code >= 500 or response.status_code == 429


def read_response(url: str, response) -> dict:
	if not isinstance(response, dict):
		log_request(url, error="M-Pesa returned a non-JSON body")
		frappe.throw(_("M-Pesa returned an unreadable response"))

	if error := response.get("errorMessage"):
		log_request(url, error=error)
		frappe.throw(_("M-Pesa rejected the request: {0}").format(error))

	response_code = response.get("ResponseCode")
	if response_code is not None and str(response_code) != MPESA_ACCEPTED_RESPONSE_CODE:
		description = response.get("ResponseDescription") or response_code
		log_request(url, error=description)
		frappe.throw(_("M-Pesa rejected the request: {0}").format(description))

	log_request(url, output=summarise_mpesa_response(response))
	return response


def log_request(url: str, output=None, error=None):
	# The payload carries the STK password, the shopper's phone number and the initiator credential,
	# so only the endpoint is logged.
	create_request_log(
		{"endpoint": url},
		service_name="M-Pesa",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)


def summarise_mpesa_response(response: dict) -> dict:
	keys = ("CheckoutRequestID", "ConversationID", "ResponseCode", "ResultCode")
	return {key: response[key] for key in keys if key in response}


def read_mpesa_error_body(exception: Exception) -> dict:
	response = getattr(exception, "response", None)
	if response is None:
		return {}
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}


def read_mpesa_error(exception: Exception) -> str | None:
	return read_mpesa_error_body(exception).get("errorMessage")


def read_mpesa_error_code(exception: Exception) -> str | None:
	return read_mpesa_error_body(exception).get("errorCode")
