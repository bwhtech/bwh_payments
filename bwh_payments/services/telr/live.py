import base64
import xml.etree.ElementTree as ElementTree

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request

from bwh_payments.services.telr.base import BaseTelrClient, TelrOrder, TelrRemoteResult

# ponytail: frappe.integrations.utils.make_request takes no timeout, so a hung Telr call holds a worker;
# revisit if Telr latency ever shows up in the request log.
TELR_BASE_URL = "https://secure.telr.com"
SETTINGS_DOCTYPE = "Telr Gateway Settings"


class LiveTelrClient(BaseTelrClient):
	def __init__(self, store_id: str, auth_key: str, test_mode: bool):
		self.store_id = store_id
		self.auth_key = auth_key
		self.test_mode = test_mode

	def create_order(self, order: dict, return_urls: dict, customer: dict) -> TelrOrder:
		response = self.post(
			{
				"method": "create",
				"store": self.store_id,
				"authkey": self.auth_key,
				"framed": 0,
				"order": order,
				"return": return_urls,
				"customer": customer,
			}
		)
		return TelrOrder.model_validate(response.get("order") or {})

	def check_order(self, order_ref: str) -> TelrOrder:
		response = self.post(
			{"method": "check", "store": self.store_id, "authkey": self.auth_key, "order": {"ref": order_ref}}
		)
		return TelrOrder.model_validate(response.get("order") or {})

	def refund(
		self, transaction_ref: str, amount: str, currency: str, remote_auth_key: str
	) -> TelrRemoteResult:
		endpoint = f"{TELR_BASE_URL}/gateway/remote.xml"
		headers = {"Content-Type": "application/xml", "Accept": "application/xml"}
		request_body = self.build_refund_request(transaction_ref, amount, currency, remote_auth_key)
		try:
			response = make_post_request(endpoint, data=request_body, headers=headers)
		except Exception as exception:
			log_request(endpoint, error=exception)
			raise

		result = read_telr_auth_response(response)
		if result.status == "A":
			log_request(endpoint, output={"status": result.status, "refund_id": result.transaction_ref})
		else:
			log_request(endpoint, output={"status": result.status}, error=result.message)
		return result

	def get_account_information(self) -> dict:
		endpoint = f"{TELR_BASE_URL}/api/v1/accounts"
		token = base64.b64encode(f"{self.store_id}:{self.auth_key}".encode()).decode()
		headers = {"accept": "application/json", "authorization": f"Basic {token}"}
		try:
			output = make_get_request(endpoint, headers=headers)
		except Exception as exception:
			log_request(endpoint, error=exception)
			raise
		log_request(endpoint, output=output)
		return output

	def build_refund_request(
		self, transaction_ref: str, amount: str, currency: str, remote_auth_key: str
	) -> str:
		remote = ElementTree.Element("remote")
		ElementTree.SubElement(remote, "store").text = str(self.store_id)
		ElementTree.SubElement(remote, "key").text = remote_auth_key
		transaction = ElementTree.SubElement(remote, "tran")
		ElementTree.SubElement(transaction, "type").text = "refund"
		ElementTree.SubElement(transaction, "class").text = "ecom"
		ElementTree.SubElement(transaction, "description").text = "Order refund"
		ElementTree.SubElement(transaction, "test").text = "1" if self.test_mode else "0"
		ElementTree.SubElement(transaction, "currency").text = currency
		ElementTree.SubElement(transaction, "amount").text = amount
		ElementTree.SubElement(transaction, "ref").text = transaction_ref
		return ElementTree.tostring(remote, encoding="unicode")

	def post(self, payload: dict) -> dict:
		endpoint = f"{TELR_BASE_URL}/gateway/order.json"
		headers = {"accept": "application/json", "Content-Type": "application/json"}
		try:
			response = make_post_request(endpoint, json=payload, headers=headers)
		except Exception as exception:
			log_request(endpoint, error=exception)
			raise

		# Telr answers 200 on failure too, so the body has to be inspected.
		if error := response.get("error"):
			log_request(endpoint, error=error.get("note") or error.get("message"))
			frappe.throw(_("Telr rejected the request: {0}").format(error.get("message")))

		log_request(endpoint, output=summarise_telr_order(response))
		return response


def log_request(endpoint: str, output=None, error=None):
	# The request payload carries the store auth key, so only the endpoint and the outcome are logged.
	create_request_log(
		{"endpoint": endpoint},
		service_name="Telr",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)


def summarise_telr_order(response) -> dict:
	"""Telr's order payloads echo the shopper's name, email and address, so only the ids are logged."""
	order = (response or {}).get("order") or {}
	return {"order_ref": order.get("ref"), "status": (order.get("status") or {}).get("text")}


def read_telr_auth_response(response) -> TelrRemoteResult:
	"""Pull status, message and transaction ref out of Telr's remote.xml reply, shape-tolerantly."""
	# ponytail: stdlib ElementTree is not hardened against XML entity attacks; the response comes from a
	# pinned Telr host over TLS. Move to defusedxml if Telr is ever fronted by a customer-controlled host.
	body = response if isinstance(response, str) else getattr(response, "text", "") or ""
	try:
		root = ElementTree.fromstring(body)
	except ElementTree.ParseError:
		return TelrRemoteResult(message=_("Telr returned an unreadable response"))

	auth = root.find("auth")
	if auth is None:
		return TelrRemoteResult(message=_("Telr returned no authorisation block"))

	def text_of(tag):
		node = auth.find(tag)
		return node.text if node is not None else None

	return TelrRemoteResult(
		status=text_of("status"), message=text_of("message"), transaction_ref=text_of("tranref")
	)
