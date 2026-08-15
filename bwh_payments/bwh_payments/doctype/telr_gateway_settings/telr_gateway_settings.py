# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import base64
import xml.etree.ElementTree as ElementTree
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.base_class import PaymentGatewayBase
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import get_minor_unit_exponent

# ponytail: frappe.integrations.utils.make_request takes no timeout, so a hung Telr call holds a worker;
# revisit if Telr latency ever shows up in the request log.
TELR_BASE_URL = "https://secure.telr.com"

# Telr reports the outcome as free text. Anything unrecognised stays Pending: never terminal, and never
# Paid, so an unmapped Telr state can neither release goods nor cancel a live order.
TELR_STATUS_MAP = {
	"paid": "Paid",
	"pending": "Pending",
	"declined": "Not Paid",
	"cancelled": "Cancelled",
	"canceled": "Cancelled",
	"expired": "Expired",
}

# Whether an authorisation is money in the bank depends on how the Telr store is set up, and Telr does not
# report which. `ecom` authorises and captures together, so leaving these Pending strands paid orders
# forever; an authorise-only store settles later, so calling them Paid ships goods against uncaptured
# funds. Hence the per-store switch, defaulting to the safe side.
TELR_AUTHORISED_STATUSES = ("authorised", "authorized")


class TelrGatewaySettings(Document, PaymentGatewayBase):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auth_key: DF.Password
		authorised_url: DF.Data | None
		cancelled_url: DF.Data | None
		currency: DF.Link | None
		declined_url: DF.Data | None
		enabled: DF.Check
		remote_auth_key: DF.Password | None
		store_id: DF.Data
		test_mode: DF.Check
		treat_authorised_as_paid: DF.Check
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "Telr"

	def get_basic_token(self) -> str:
		credentials = f"{self.store_id}:{self.get_password('auth_key')}"
		return base64.b64encode(credentials.encode()).decode()

	@frappe.whitelist()
	def get_account_information(self):
		endpoint = f"{TELR_BASE_URL}/api/v1/accounts"
		headers = {"accept": "application/json", "authorization": f"Basic {self.get_basic_token()}"}
		try:
			output = make_get_request(endpoint, headers=headers)
		except Exception as exception:
			self.log_request(endpoint, error=exception)
			raise
		self.log_request(endpoint, output=output)
		return output

	def validate_transaction_currency(self, currency: str):
		if not frappe.db.exists("Currency", currency):
			frappe.throw(_("{0} is not a currency configured on this site").format(frappe.bold(currency)))

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> dict:
		currency = currency or self.currency
		self.validate_transaction_currency(currency)

		endpoint = f"{TELR_BASE_URL}/gateway/order.json"
		payload = {
			"method": "create",
			"store": self.store_id,
			"authkey": self.get_password("auth_key"),
			"framed": 0,
			"order": {
				"cartid": reference,
				"test": "1" if self.test_mode else "0",
				"amount": str(flt(amount, get_minor_unit_exponent(currency))),
				"currency": currency,
				"description": _("Online order payment"),
			},
			"return": {
				"authorised": self.build_return_url(self.authorised_url, reference),
				"declined": get_localised_url(self.declined_url),
				"cancelled": get_localised_url(self.cancelled_url),
			},
			"customer": customer or {},
		}

		response = self.post(endpoint, payload)
		order = response.get("order") or {}
		return {
			"session_id": order.get("ref"),
			"redirect_url": order.get("url"),
			"success_url": payload["return"]["authorised"],
			"cancel_url": payload["return"]["cancelled"],
			"failure_url": payload["return"]["declined"],
		}

	def build_return_url(self, url: str, reference: str | None) -> str:
		"""Telr cannot echo its own order ref back, so the shopper returns with our request name."""
		if not url:
			frappe.throw(_("Please set the Authorised URL in Telr Gateway Settings"))
		parts = urlsplit(get_localised_url(url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_order(self, session_id: str) -> dict:
		endpoint = f"{TELR_BASE_URL}/gateway/order.json"
		payload = {
			"method": "check",
			"store": self.store_id,
			"authkey": self.get_password("auth_key"),
			"order": {"ref": session_id},
		}
		return (self.post(endpoint, payload).get("order")) or {}

	def get_payment_status(self, session_id: str) -> str:
		order = self.get_order(session_id)
		status_text = ((order.get("status") or {}).get("text") or "").strip().casefold()
		if status_text in TELR_AUTHORISED_STATUSES:
			return "Paid" if self.treat_authorised_as_paid else "Pending"
		return TELR_STATUS_MAP.get(status_text, "Pending")

	def get_transaction_reference(self, session_id: str) -> str:
		transaction = self.get_order(session_id).get("transaction") or {}
		reference = transaction.get("ref")
		if not reference:
			frappe.throw(_("Telr has no settled transaction for this order; it cannot be refunded."))
		return reference

	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		# Telr has no signed webhook — status is only ever taken from the authenticated `check` API in
		# get_payment_status. Ignoring the delivery is what keeps an unverified caller from marking a
		# request Paid.
		return {}

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> dict:
		currency = currency or self.currency
		remote_auth_key = self.get_password("remote_auth_key")
		if not remote_auth_key:
			frappe.throw(_("Please set the Remote Auth Key in Telr Gateway Settings to issue refunds"))

		endpoint = f"{TELR_BASE_URL}/gateway/remote.xml"
		request_body = self.build_refund_request(
			self.get_transaction_reference(session_id), amount, currency, remote_auth_key
		)
		headers = {"Content-Type": "application/xml", "Accept": "application/xml"}

		try:
			response = make_post_request(endpoint, data=request_body, headers=headers)
		except Exception as exception:
			self.log_request(endpoint, error=exception)
			raise

		status, message, refund_id = read_telr_auth_response(response)
		if status != "A":
			self.log_request(endpoint, output={"status": status}, error=message)
			frappe.throw(_("Telr refused the refund: {0}").format(message or _("unknown error")))

		self.log_request(endpoint, output={"status": status, "refund_id": refund_id})
		return {"refund_id": refund_id, "status": "succeeded", "amount": flt(amount)}

	def build_refund_request(
		self, transaction_reference: str, amount: float, currency: str, remote_auth_key: str
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
		ElementTree.SubElement(transaction, "amount").text = str(
			flt(amount, get_minor_unit_exponent(currency))
		)
		ElementTree.SubElement(transaction, "ref").text = transaction_reference
		return ElementTree.tostring(remote, encoding="unicode")

	def post(self, endpoint: str, payload: dict) -> dict:
		headers = {"accept": "application/json", "Content-Type": "application/json"}
		try:
			response = make_post_request(endpoint, json=payload, headers=headers)
		except Exception as exception:
			self.log_request(endpoint, error=exception)
			raise

		# Telr answers 200 on failure too, so the body has to be inspected.
		if error := response.get("error"):
			self.log_request(endpoint, error=error.get("note") or error.get("message"))
			frappe.throw(_("Telr rejected the request: {0}").format(error.get("message")))

		self.log_request(endpoint, output=summarise_telr_order(response))
		return response

	def log_request(self, endpoint: str, output=None, error=None):
		# The request payload carries the store auth key, so only the endpoint and the outcome are logged.
		create_request_log(
			{"endpoint": endpoint},
			service_name="Telr",
			is_remote_request=True,
			reference_doctype=self.doctype,
			reference_docname=self.name,
			output=output,
			error=error,
			status="Failed" if error else "Completed",
		)


def summarise_telr_order(response) -> dict:
	"""Telr's order payloads echo the shopper's name, email and address, so only the ids are logged."""
	order = (response or {}).get("order") or {}
	return {"order_ref": order.get("ref"), "status": (order.get("status") or {}).get("text")}


def read_telr_auth_response(response) -> tuple[str | None, str | None, str | None]:
	"""Pull status, message and transaction ref out of Telr's remote.xml reply, shape-tolerantly."""
	# ponytail: stdlib ElementTree is not hardened against XML entity attacks; the response comes from a
	# pinned Telr host over TLS. Move to defusedxml if Telr is ever fronted by a customer-controlled host.
	body = response if isinstance(response, str) else getattr(response, "text", "") or ""
	try:
		root = ElementTree.fromstring(body)
	except ElementTree.ParseError:
		return None, _("Telr returned an unreadable response"), None

	auth = root.find("auth")
	if auth is None:
		return None, _("Telr returned no authorisation block"), None

	def text_of(tag):
		node = auth.find(tag)
		return node.text if node is not None else None

	return text_of("status"), text_of("message"), text_of("tranref")
