"""Entry points for other apps. Nothing here checks permissions, so never whitelist these."""

import frappe
from frappe import _

from bwh_payments.base_class import GatewayInfo, GatewayOption
from bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request import (
	GatewayPaymentRequest,
)
from bwh_payments.bwh_payments.doctype.gateway_refund.gateway_refund import normalise_refund_status
from bwh_payments.bwh_payments.utils import (
	get_available_payment_modes,
	get_gateway_settings,
	get_gateway_settings_doctypes,
	resolve_payment_mode,
	supports_currency,
)
from bwh_payments.currency import from_minor_units, to_minor_units, validate_transaction_currency

__all__ = [
	"GatewayInfo",
	"GatewayOption",
	"create_payment",
	"from_minor_units",
	"get_gateway_options",
	"get_gateways",
	"get_payment_gateways",
	"record_external_payment",
	"record_external_refund",
	"refund",
	"resolve_payment_mode",
	"sync_refunds",
	"to_minor_units",
	"validate_currency",
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


def get_gateway_options(currency: str | None = None) -> list[GatewayOption]:
	"""Enabled gateways, with logos, that can take `currency` (every enabled one when None)."""
	options = []
	for gateway in get_available_payment_modes():
		settings = get_gateway_settings(gateway)
		if currency and not supports_currency(settings, currency):
			continue
		options.append(
			GatewayOption(
				name=gateway, title=gateway, logo=settings.logo, logo_dark=settings.logo_dark or settings.logo
			)
		)
	return options


def get_gateways() -> list[GatewayInfo]:
	"""Every gateway installed on the site, enabled or not, described for a settings screen."""
	gateways = []
	for doctype in get_gateway_settings_doctypes():
		settings = frappe.get_cached_doc(doctype)
		gateways.append(
			GatewayInfo(
				name=settings.get_gateway_name(),
				settings_doctype=doctype,
				description=settings.description,
				docs_url=settings.docs_url,
				reusable_checkout=settings.reusable_checkout,
				logo=settings.logo,
				logo_dark=settings.logo_dark or settings.logo,
			)
		)
	return sorted(gateways, key=lambda gateway: gateway.name.casefold())


def validate_currency(gateway: str, currency: str):
	"""Throw unless `gateway` can charge in `currency`, before any checkout is opened."""
	validate_transaction_currency(currency)
	if not supports_currency(get_gateway_settings(gateway), currency):
		frappe.throw(_("{0} cannot take payments in {1}").format(frappe.bold(gateway), frappe.bold(currency)))


def refund(payment_request: str, amount: float | None = None, reference: str | None = None):
	"""Refund `amount` (the whole remaining balance when None) and return the Gateway Refund.

	`reference` is the caller's own id for the refund; `on_refund_update(refund)` on the referenced
	document hears each status change, so it can find its record by `refund.reference`.
	"""
	return frappe.get_doc("Gateway Payment Request", payment_request).create_refund(
		amount, reference=reference
	)


def sync_refunds(payment_request: str):
	frappe.get_doc("Gateway Payment Request", payment_request).update_pending_refunds()


def record_external_payment(
	ref_doctype: str,
	ref_docname: str,
	amount: float,
	currency: str,
	gateway: str,
	status: str,
	order_ref: str,
	transaction_ref: str | None = None,
) -> GatewayPaymentRequest:
	"""Record a payment taken outside this app, e.g. when importing from another payments app.

	No checkout is opened and the referenced document is not settled again. Idempotent on `order_ref`.
	"""
	if name := frappe.db.get_value("Gateway Payment Request", {"order_ref": order_ref}, "name"):
		return frappe.get_doc("Gateway Payment Request", name)

	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"ref_doctype": ref_doctype,
			"ref_docname": ref_docname,
			"amount": amount,
			"currency_code": currency,
			"gateway": gateway,
			"status": status,
			"order_ref": order_ref,
			"gateway_transaction_ref": transaction_ref,
		}
	).insert(ignore_permissions=True)


def record_external_refund(
	payment_request: str, refund_id: str, status: str, amount: float, reference: str | None = None
):
	"""Record a refund settled outside this app. Idempotent on `refund_id`; no callback runs."""
	if name := frappe.db.get_value("Gateway Refund", {"refund_id": refund_id}, "name"):
		return frappe.get_doc("Gateway Refund", name)

	gateway_refund = frappe.get_doc(
		{
			"doctype": "Gateway Refund",
			"payment_request": payment_request,
			"refund_id": refund_id,
			"status": normalise_refund_status(status),
			"amount": amount,
			"reference": reference,
		}
	)
	gateway_refund.flags.imported = True
	return gateway_refund.insert(ignore_permissions=True)
