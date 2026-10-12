import frappe
from frappe import _

from bwh_payments.bwh_payments.braintree_checkout import get_payable_request
from bwh_payments.bwh_payments.doctype.braintree_gateway_settings.braintree_gateway_settings import (
	format_amount,
)

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.title = _("Pay by card")
	session = frappe.form_dict.get("session")

	try:
		payment_request = get_payable_request(session)
	except frappe.PermissionError:
		frappe.clear_messages()
		context.error = _("This payment link is not valid.")
		return context

	if payment_request.status == "Paid":
		frappe.local.flags.redirect_location = payment_request.success_url
		raise frappe.Redirect
	if payment_request.status != "Pending":
		context.error = _("This payment is no longer open.")
		context.cancel_url = payment_request.cancel_url
		return context

	settings = payment_request.get_gateway_settings()
	currency = payment_request.currency_code
	context.session = session
	context.amount = format_amount(payment_request.amount, currency)
	context.currency = currency
	context.email = payment_request.customer_email or ""
	context.three_d_secure = bool(settings.three_d_secure)
	context.cancel_url = payment_request.cancel_url
	context.client_token = settings.get_client().generate_client_token(
		settings.get_merchant_account(currency)
	)
	return context
