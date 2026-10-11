"""Entry points for other apps. Nothing here checks permissions, so never whitelist these."""

import frappe
from frappe import _

from bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request import (
	GatewayPaymentRequest,
)
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
) -> GatewayPaymentRequest:
	"""Send the shopper to the returned request's `order_url`. The referenced document's
	`on_payment_authorized(status)` runs once the payment is Paid.

	With `redirect_to` (a path on this site), the gateway returns the shopper through
	`checkout.return_from_gateway`, which confirms the payment before redirecting there.
	"""
	# The insert skips permissions, so user input must not reach status or the ledger.
	allowed = ("company", "redirect_to")
	if blocked := [field for field in fields if field not in allowed and not field.startswith("customer_")]:
		frappe.throw(_("create_payment cannot set {0}").format(", ".join(sorted(blocked))))

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
