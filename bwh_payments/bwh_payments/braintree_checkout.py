import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from bwh_payments.bwh_payments.doctype.braintree_gateway_settings.braintree_gateway_settings import (
	acquire_session_lock,
	release_session_lock,
)

SETTINGS_DOCTYPE = "Braintree Gateway Settings"


# nosemgrep: guest-whitelisted-method  # the 32-character session token is the capability
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60, ip_based=True)
def pay(session: str, nonce: str) -> dict:
	"""Charge the request behind `session` once. The amount comes from the request, never the browser.

	Returns `{"redirect_url"}` once paid, or `{"error"}` for a decline the shopper can retry.
	"""
	get_payable_request(session)
	acquire_session_lock(session)
	try:
		# Nothing is written yet. A fresh snapshot after the wait, or MariaDB's snapshot isolation fails
		# the row lock below with "Record has changed" once the first attempt commits.
		frappe.db.commit()  # nosemgrep
		result = charge_request(get_payable_request(session), nonce)
		# Committed before the lock is released, so a second attempt sees this sale and does not charge.
		frappe.db.commit()  # nosemgrep
		return result
	finally:
		release_session_lock(session)


def charge_request(payment_request, nonce: str) -> dict:
	payment_request.lock_row()
	if payment_request.status == "Paid":
		return {"redirect_url": payment_request.success_url}
	if payment_request.status != "Pending":
		frappe.throw(_("This payment is no longer open."))

	settings = payment_request.get_gateway_settings()
	# A sale an earlier attempt made, if the shopper retried before the page caught up.
	if settings.get_payment_status(payment_request.order_ref) != "Paid":
		result = settings.charge(
			payment_request.order_ref, payment_request.amount, payment_request.currency_code, nonce
		)
		if not result.is_success:
			return {
				"error": _("The payment was declined: {0}. Please try another card.").format(result.message)
			}
		payment_request.db_set("gateway_transaction_ref", result.transaction.id, update_modified=False)
		# The money has moved: keep the record even if placing the order below fails.
		frappe.db.commit()  # nosemgrep

	# Placed here rather than by the return page, so closing the tab after paying still places the order.
	try:
		# As the shopper, not Administrator: the order's app elevates itself and stamps the shopper as owner.
		payment_request.sync_status()
	except Exception:
		# The shopper has paid, so they still go to the confirmation page, whose sync retries the order.
		frappe.db.rollback()
		frappe.log_error(title=f"Braintree order placement failed for {payment_request.name}")
	return {"redirect_url": payment_request.success_url}


def get_payable_request(session: str):
	name = frappe.db.get_value("Gateway Payment Request", {"order_ref": session}, "name") if session else None
	payment_request = frappe.get_doc("Gateway Payment Request", name) if name else None
	if not payment_request or payment_request.get_gateway_settings().doctype != SETTINGS_DOCTYPE:
		frappe.throw(_("This payment link is not valid."), frappe.PermissionError)
	return payment_request
