# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
import stripe
from frappe import _
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.base_class import PaymentGatewayBase
from bwh_payments.currency import from_minor_units, to_minor_units, validate_transaction_currency

# Stripe substitutes this itself on redirect, so it has to survive URL encoding intact.
STRIPE_SESSION_ID_PLACEHOLDER = "{CHECKOUT_SESSION_ID}"


class StripeGatewaySettings(Document, PaymentGatewayBase):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		failure_url: DF.Data | None
		mode: DF.Literal["Test", "Live"]
		private_key: DF.Password
		public_key: DF.Data
		success_url: DF.Data | None
		webhook_secret: DF.Password
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "Stripe"

	def get_client(self) -> stripe.StripeClient:
		# A client per call keeps two configured accounts from clobbering each other through the module
		# level `stripe.api_key` that the SDK otherwise reads.
		return stripe.StripeClient(self.get_password("private_key"))

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> dict:
		validate_transaction_currency(currency)
		session = self.get_client().checkout.sessions.create(
			{
				"mode": "payment",
				"client_reference_id": reference,
				"customer_email": (customer or {}).get("email") or None,
				"line_items": [
					{
						"price_data": {
							"currency": currency.lower(),
							"product_data": {"name": _("Order Payment")},
							"unit_amount": to_minor_units(amount, currency),
						},
						"quantity": 1,
					}
				],
				"success_url": self.build_success_url(),
				"cancel_url": self.failure_url,
			}
		)
		return {
			"session_id": session.id,
			"redirect_url": session.url,
			"success_url": session.success_url,
			"cancel_url": session.cancel_url,
			"failure_url": session.cancel_url,
		}

	def build_success_url(self) -> str:
		# String-concatenating "?session_id=..." breaks any success URL that already carries a query.
		if not self.success_url:
			frappe.throw(_("Please set a Success URL in Stripe Gateway Settings"))
		parts = urlsplit(self.success_url)
		encoded_query = urlencode(parse_qsl(parts.query))
		if encoded_query:
			encoded_query += "&"
		encoded_query += f"session_id={STRIPE_SESSION_ID_PLACEHOLDER}"
		return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded_query, parts.fragment))

	def get_payment_status(self, session_id: str) -> str:
		session = self.get_client().checkout.sessions.retrieve(session_id)
		if session.payment_status == "paid":
			return "Paid"
		if session.status == "expired":
			return "Expired"
		return "Pending"

	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		webhook_secret = self.get_password("webhook_secret")
		if not webhook_secret:
			frappe.throw(_("Webhook secret is not configured in Stripe Gateway Settings"))

		signature = headers.get("Stripe-Signature")
		if not signature:
			frappe.throw(_("Missing Stripe-Signature header"))

		event = stripe.Webhook.construct_event(payload, signature, webhook_secret)

		if event["type"] != "checkout.session.completed":
			return {}

		session = event["data"]["object"]
		return {
			"session_id": session["id"],
			"status": "Paid" if session["payment_status"] == "paid" else "Pending",
			"event_id": event["id"],
		}

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> dict:
		client = self.get_client()
		session = client.checkout.sessions.retrieve(session_id)

		if not session.payment_intent:
			frappe.throw(_("No payment intent found for this session; the payment did not complete."))

		currency = currency or session.currency
		refund = client.refunds.create(
			{
				"payment_intent": session.payment_intent,
				"amount": to_minor_units(amount, currency),
			}
		)

		if refund.status not in ("succeeded", "pending"):
			frappe.throw(_("Refund failed with status: {0}").format(refund.status))

		return {
			"refund_id": refund.id,
			"status": refund.status,
			"amount": flt(from_minor_units(refund.amount, currency)),
		}
