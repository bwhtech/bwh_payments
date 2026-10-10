import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.rate_limiter import rate_limit

from bwh_payments.base_class import WebhookEvent

WEBHOOK_ACCEPTED = {"status": "ok"}


# nosemgrep: guest-whitelisted-method  # gateways post unauthenticated; each verifier checks the signature
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60, ip_based=True)
def handle():
	gateway = frappe.request.args.get("gateway")
	if not gateway:
		return reject()

	profile = frappe.db.get_value(
		"Payment Gateway Profile", gateway, ["name", "enabled", "gateway_settings"], as_dict=True
	)
	if not profile or not profile.enabled:
		return reject()

	payload = frappe.request.get_data()
	headers = dict(frappe.request.headers)

	try:
		event = frappe.get_single(profile.gateway_settings).handle_webhook(payload, headers)
		# Inside the try: an unparseable verified event is still a 400, so the gateway retries.
		event = WebhookEvent.model_validate(event) if event else None
	except Exception:
		# Never log the payload: it can carry cardholder data.
		frappe.log_error(title=f"{gateway} webhook verification failed")
		log_webhook(gateway, status="Failed")
		return reject()

	if not event:
		log_webhook(gateway, status="Completed")
		return WEBHOOK_ACCEPTED

	request_name = frappe.db.get_value("Gateway Payment Request", {"order_ref": event.session_id}, "name")
	if not request_name:
		log_webhook(gateway, session_id=event.session_id, event_id=event.event_id, status="Failed")
		return WEBHOOK_ACCEPTED

	payment_request = frappe.get_doc("Gateway Payment Request", request_name)
	if payment_request.gateway != gateway:
		frappe.log_error(
			title=f"{gateway} webhook gateway mismatch",
			message=f"URL gateway: {gateway}, request gateway: {payment_request.gateway}",
		)
		log_webhook(gateway, session_id=event.session_id, event_id=event.event_id, status="Failed")
		return reject()

	apply_status_as_administrator(payment_request, event.status, event.event_id)
	log_webhook(gateway, session_id=event.session_id, event_id=event.event_id, status="Completed")
	return WEBHOOK_ACCEPTED


def apply_status_as_administrator(payment_request, status: str, event_id: str | None):
	# Downstream order creation needs a real user. Restore after, so the worker does not stay Administrator.
	session_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		payment_request.apply_webhook_status(status, event_id)
	finally:
		frappe.set_user(session_user)


def reject() -> dict:
	"""One opaque 400 for every failure, so callers cannot probe gateways or verifiers.

	The message log is cleared so a verifier's `frappe.throw` does not leak in `_server_messages`.
	"""
	frappe.local.message_log = []
	frappe.local.response["http_status_code"] = 400
	return {"status": "error", "message": _("Webhook could not be verified")}


def log_webhook(gateway: str, session_id: str | None = None, event_id: str | None = None, status="Queued"):
	create_request_log(
		{"gateway": gateway, "session_id": session_id, "event_id": event_id},
		service_name=f"{gateway} Webhook",
		status=status,
		reference_doctype="Gateway Payment Request",
	)
