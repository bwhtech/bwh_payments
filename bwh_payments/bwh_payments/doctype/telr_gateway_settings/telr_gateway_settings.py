# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.base_class import CheckoutSession, PaymentGatewayBase, RefundResult
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency
from bwh_payments.services.telr.base import BaseTelrClient
from bwh_payments.services.telr.live import LiveTelrClient
from bwh_payments.services.telr.stub import StubTelrClient

# Telr reports free text. Unmapped states stay Pending.
TELR_STATUS_MAP = {
	"paid": "Paid",
	"pending": "Pending",
	"declined": "Not Paid",
	"cancelled": "Cancelled",
	"canceled": "Cancelled",
	"expired": "Expired",
}

# Telr does not say whether the store captures on authorisation, hence `treat_authorised_as_paid`.
TELR_AUTHORISED_STATUSES = ("authorised", "authorized")
TELR_ACCEPTED_STATUS = "A"


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

	def get_client(self) -> BaseTelrClient:
		if frappe.in_test:
			return StubTelrClient()

		return LiveTelrClient(self.store_id, self.get_password("auth_key"), bool(self.test_mode))

	@frappe.whitelist()
	def get_account_information(self):
		return self.get_client().get_account_information()

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> CheckoutSession:
		currency = currency or self.currency
		validate_transaction_currency(currency)

		return_urls = {
			"authorised": self.build_return_url(self.authorised_url, reference),
			"declined": get_localised_url(self.declined_url),
			"cancelled": get_localised_url(self.cancelled_url),
		}
		order = self.get_client().create_order(
			{
				"cartid": reference,
				"test": "1" if self.test_mode else "0",
				"amount": format_telr_amount(amount, currency),
				"currency": currency,
				"description": _("Online order payment"),
			},
			return_urls,
			customer or {},
		)
		return CheckoutSession(
			session_id=order.ref,
			redirect_url=order.url,
			success_url=return_urls["authorised"],
			cancel_url=return_urls["cancelled"],
			failure_url=return_urls["declined"],
		)

	def build_return_url(self, url: str, reference: str | None) -> str:
		"""Telr does not echo its order ref, so add our request name."""
		if not url:
			frappe.throw(_("Please set the Authorised URL in Telr Gateway Settings"))
		parts = urlsplit(get_localised_url(url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_payment_status(self, session_id: str) -> str:
		order = self.get_client().check_order(session_id)
		status_text = ((order.status.text if order.status else None) or "").strip().casefold()
		if status_text in TELR_AUTHORISED_STATUSES:
			return "Paid" if self.treat_authorised_as_paid else "Pending"
		return TELR_STATUS_MAP.get(status_text, "Pending")

	def get_transaction_reference(self, client: BaseTelrClient, session_id: str) -> str:
		transaction = client.check_order(session_id).transaction
		if not (transaction and transaction.ref):
			frappe.throw(_("Telr has no settled transaction for this order; it cannot be refunded."))
		return transaction.ref

	def handle_webhook(self, payload: bytes, headers: dict) -> None:
		# Telr webhooks are unsigned; status comes only from get_payment_status.
		return None

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		currency = currency or self.currency
		remote_auth_key = self.get_password("remote_auth_key")
		if not remote_auth_key:
			frappe.throw(_("Please set the Remote Auth Key in Telr Gateway Settings to issue refunds"))

		client = self.get_client()
		result = client.refund(
			self.get_transaction_reference(client, session_id),
			format_telr_amount(amount, currency),
			currency,
			remote_auth_key,
		)
		if result.status != TELR_ACCEPTED_STATUS:
			frappe.throw(_("Telr refused the refund: {0}").format(result.message or _("unknown error")))

		return RefundResult(refund_id=result.transaction_ref, status="succeeded", amount=flt(amount))


def format_telr_amount(amount: float, currency: str) -> str:
	return str(flt(amount, get_minor_unit_exponent(currency)))
