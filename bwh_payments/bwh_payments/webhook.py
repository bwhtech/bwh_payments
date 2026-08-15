import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.rate_limiter import rate_limit

WEBHOOK_ACCEPTED = {"status": "ok"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60, ip_based=True)
def handle():
	"""Public gateway callback. Everything here is untrusted until the gateway's own signature clears it."""
	gateway = frappe.request.args.get("gateway")
	if not gateway:
		return reject(_("Gateway parameter is required"))

	profile = frappe.db.get_value(
		"Payment Gateway Profile", gateway, ["name", "enabled", "gateway_settings"], as_dict=True
	)
	if not profile or not profile.enabled:
		return reject(_("Unknown or disabled gateway"))

	payload = frappe.request.get_data()
	headers = dict(frappe.request.headers)

	try:
		result = frappe.get_single(profile.gateway_settings).handle_webhook(payload, headers)
	except Exception:
		# The payload can carry cardholder data, so only the gateway and the traceback are recorded.
		frappe.log_error(title=f"{gateway} webhook verification failed")
		log_webhook(gateway, status="Failed")
		return reject(_("Webhook could not be verified"))

	if not result:
		log_webhook(gateway, status="Completed")
		return WEBHOOK_ACCEPTED

	session_id = result.get("session_id")
	status = result.get("status")
	event_id = result.get("event_id")

	if not (session_id and status):
		log_webhook(gateway, event_id=event_id, status="Failed")
		return WEBHOOK_ACCEPTED

	request_name = frappe.db.get_value("Gateway Payment Request", {"order_ref": session_id}, "name")
	if not request_name:
		log_webhook(gateway, session_id=session_id, event_id=event_id, status="Failed")
		return WEBHOOK_ACCEPTED

	payment_request = frappe.get_doc("Gateway Payment Request", request_name)
	if payment_request.gateway != gateway:
		frappe.log_error(
			title=f"{gateway} webhook gateway mismatch",
			message=f"URL gateway: {gateway}, request gateway: {payment_request.gateway}",
		)
		log_webhook(gateway, session_id=session_id, event_id=event_id, status="Failed")
		return reject(_("Gateway mismatch"))

	apply_status_as_administrator(payment_request, status, event_id)
	log_webhook(gateway, session_id=session_id, event_id=event_id, status="Completed")
	# A replayed delivery is still a success as far as the gateway is concerned; anything else and it
	# keeps retrying forever.
	return WEBHOOK_ACCEPTED


def apply_status_as_administrator(payment_request, status: str, event_id: str | None):
	# The callback arrives as Guest, but downstream order creation needs a real user context. Restore the
	# original user afterwards so a long-lived worker does not keep Administrator for the next job.
	session_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		payment_request.apply_webhook_status(status, event_id)
	finally:
		frappe.set_user(session_user)


def reject(message: str) -> dict:
	frappe.local.response["http_status_code"] = 400
	return {"status": "error", "message": message}


def log_webhook(gateway: str, session_id: str | None = None, event_id: str | None = None, status="Queued"):
	create_request_log(
		{"gateway": gateway, "session_id": session_id, "event_id": event_id},
		service_name=f"{gateway} Webhook",
		status=status,
		reference_doctype="Gateway Payment Request",
	)
