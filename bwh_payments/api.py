"""The supported entry points for other apps. Server-side only: nothing here checks permissions, so never
whitelist these as they stand."""

import frappe

from bwh_payments.bwh_payments.utils import get_available_payment_modes, resolve_payment_mode
from bwh_payments.currency import from_minor_units, to_minor_units

__all__ = [
	"create_payment",
	"from_minor_units",
	"get_payment_gateways",
	"resolve_payment_mode",
	"to_minor_units",
]

get_payment_gateways = get_available_payment_modes


def create_payment(
	ref_doctype: str, ref_docname: str, amount: float, currency: str, gateway: str, **fields
) -> "frappe.model.document.Document":
	"""Open a gateway checkout for a document and return its Gateway Payment Request.

	Send the shopper to the request's `order_url`. `fields` takes any other request field, such as
	`company` or the `customer_*` details the gateway prefills. The referenced document's
	`on_payment_authorized(status)` runs once the payment is Paid.
	"""
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"ref_doctype": ref_doctype,
			"ref_docname": ref_docname,
			"amount": amount,
			"currency_code": currency,
			"gateway": gateway,
			**fields,
		}
	).insert(ignore_permissions=True)
