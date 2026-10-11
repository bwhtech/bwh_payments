import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

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
		try:
			with run_as_administrator():
				payment_request.sync_status()
		except Exception:
			# The shopper still goes on; the webhook settles the payment later.
			frappe.db.rollback()
			frappe.clear_last_message()
			frappe.log_error(title=_("Could not confirm payment {0} on return").format(request))

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = redirect_to
