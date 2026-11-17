import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from bwh_payments.base_class import InlineCheckoutGateway
from bwh_payments.bwh_payments.utils import run_as_administrator

RETURN_ENDPOINT = "/api/method/bwh_payments.bwh_payments.checkout.return_from_gateway"


def get_return_url(payment_request: str) -> str:
	return frappe.utils.get_url(f"{RETURN_ENDPOINT}?request={payment_request}")


# nosemgrep: guest-whitelisted-method  # the gateway sends the shopper's browser here; it only reads the gateway
@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=60, seconds=60, ip_based=True)
def return_from_gateway(request: str):
	"""Confirm the payment with the gateway, then send the shopper on to the caller's page.

	The browser's arrival proves nothing, so the status is read from the gateway, never from the URL.
	"""
	redirect_to = frappe.db.get_value("Gateway Payment Request", request, "redirect_to")
	if not redirect_to:
		raise frappe.DoesNotExistError(_("Payment not found"))

	payment_request = frappe.get_doc("Gateway Payment Request", request)
	if payment_request.status == "Pending":
		sync_status_safely(payment_request)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = redirect_to


def sync_status_safely(payment_request):
	try:
		with run_as_administrator():
			payment_request.sync_status()
		# Frappe rolls back GET requests; the gateway's answer must be kept.
		frappe.local.flags.commit = True
	except Exception:
		# The shopper still goes on; the webhook settles the payment later.
		frappe.db.rollback()
		frappe.clear_last_message()
		frappe.log_error(title=_("Could not confirm payment {0}").format(payment_request.name))


# nosemgrep: guest-whitelisted-method  # a guest pays in the page; the gateway's signature proves the response
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60, ip_based=True)
def confirm_inline_payment(request: str, response: dict) -> str:
	"""Check the SDK's signed response, confirm the payment with the gateway, and return `redirect_to`.

	The signature only ties the response to this checkout; the status is still read from the gateway.
	"""
	payment_request = get_verified_inline_request(request, response)
	if payment_request.status == "Pending":
		sync_status_safely(payment_request)
	return payment_request.redirect_to


def get_verified_inline_request(request: str, response: dict):
	# One error for every failure, so a guest learns nothing about which requests exist.
	payment_request = frappe.db.exists("Gateway Payment Request", request) and frappe.get_doc(
		"Gateway Payment Request", request
	)
	if not (payment_request and payment_request.order_ref):
		frappe.throw(_("The payment could not be verified"), frappe.PermissionError)
	settings = payment_request.get_gateway_settings()
	if not (
		isinstance(settings, InlineCheckoutGateway)
		and settings.verify_inline_payment(payment_request.order_ref, response)
	):
		frappe.throw(_("The payment could not be verified"), frappe.PermissionError)
	return payment_request
