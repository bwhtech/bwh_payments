import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.rate_limiter import rate_limit

from bwh_payments.base_class import WebhookEvent

WEBHOOK_ACCEPTED = {"status": "ok"}
WEBHOOK_SAVEPOINT = "bwh_payments_webhook"


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

	request_name = find_payment_request(event)
	if not request_name:
		log_webhook(gateway, event=event, status="Failed")
		return WEBHOOK_ACCEPTED

	payment_request = frappe.get_doc("Gateway Payment Request", request_name)
	if payment_request.gateway != gateway:
		frappe.log_error(
			title=f"{gateway} webhook gateway mismatch",
			message=f"URL gateway: {gateway}, request gateway: {payment_request.gateway}",
		)
		log_webhook(gateway, event=event, status="Failed")
		return reject()

	# The gateway resends until it gets a 2xx, so a redelivery must not queue the event twice.
	if event.event_id and frappe.db.exists(
		"Integration Request",
		{"integration_request_service": f"{gateway} Webhook", "request_id": event.event_id},
	):
		return WEBHOOK_ACCEPTED

	log = log_webhook(gateway, event=event, status="Queued", reference_docname=request_name)
	enqueue_webhook(log.name)
	return WEBHOOK_ACCEPTED


def find_payment_request(event: WebhookEvent) -> str | None:
	if not event.refund_id:
		return frappe.db.get_value("Gateway Payment Request", {"order_ref": event.session_id}, "name")

	if request_name := frappe.db.get_value(
		"Gateway Refund", {"refund_id": event.refund_id}, "payment_request"
	):
		return request_name
	# A refund raised in the gateway's dashboard is only known by the payment it refunds.
	if event.transaction_ref:
		return frappe.db.get_value(
			"Gateway Payment Request", {"gateway_transaction_ref": event.transaction_ref}, "name"
		)
	return None


def enqueue_webhook(integration_request: str):
	frappe.enqueue(
		"bwh_payments.bwh_payments.webhook.process_webhook",
		queue="short",
		integration_request=integration_request,
		now=frappe.in_test,
	)


def process_webhook(integration_request: str):
	"""Apply a verified event that `handle` logged and queued.

	It runs in a job so a broken referenced document cannot turn the gateway's delivery into a 500
	that it retries until it disables the endpoint. A failure is logged and can be retried with
	`retry_webhook` once fixed.
	"""
	log = frappe.get_doc("Integration Request", integration_request)
	event = WebhookEvent.model_validate(frappe.parse_json(log.data))

	frappe.db.savepoint(WEBHOOK_SAVEPOINT)
	try:
		payment_request = frappe.get_doc("Gateway Payment Request", log.reference_docname)
		apply_event_as_administrator(payment_request, event)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as error:
		frappe.db.rollback(save_point=WEBHOOK_SAVEPOINT)
		# Frappe's job runner retries only RetryBackgroundJobError, not the DB errors it wraps.
		raise frappe.RetryBackgroundJobError from error
	except Exception as error:
		frappe.db.rollback(save_point=WEBHOOK_SAVEPOINT)
		frappe.log_error(title=f"{log.integration_request_service} could not be applied")
		log.handle_failure({"error": repr(error)})
		return

	log.db_set("status", "Completed")


@frappe.whitelist(methods=["POST"])
def retry_webhook(integration_request: str) -> None:
	frappe.only_for("System Manager")

	log = frappe.get_doc("Integration Request", integration_request)
	if log.reference_doctype != "Gateway Payment Request" or log.status not in ("Queued", "Failed"):
		frappe.throw(_("Only a queued or failed payment webhook can be retried"))

	log.db_set("status", "Queued")
	enqueue_webhook(log.name)


def apply_event_as_administrator(payment_request, event: WebhookEvent):
	# Downstream order creation needs a real user. Restore after, so the worker does not stay Administrator.
	session_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		if event.refund_id:
			apply_refund_event(payment_request, event)
		else:
			payment_request.apply_webhook_status(event.status, event.event_id)
	finally:
		frappe.set_user(session_user)


def apply_refund_event(payment_request, event: WebhookEvent):
	if name := frappe.db.get_value("Gateway Refund", {"refund_id": event.refund_id}, "name"):
		refund = frappe.get_doc("Gateway Refund", name)
	elif frappe.db.exists(
		"Gateway Refund",
		{"payment_request": payment_request.name, "refund_id": ["is", "not set"], "status": "Pending"},
	):
		# One of our refunds is still waiting for the gateway's answer and may be this one; recording
		# it now as a stranger would count it twice.
		raise frappe.RetryBackgroundJobError
	else:
		refund = payment_request.record_external_refund(event.refund_id, event.refund_amount)

	refund.apply_gateway_status(event.status, event.event_id)


def reject() -> dict:
	"""One opaque 400 for every failure, so callers cannot probe gateways or verifiers.

	The message log is cleared so a verifier's `frappe.throw` does not leak in `_server_messages`.
	"""
	frappe.local.message_log = []
	frappe.local.response["http_status_code"] = 400
	return {"status": "error", "message": _("Webhook could not be verified")}


def log_webhook(
	gateway: str,
	status: str = "Queued",
	event: WebhookEvent | None = None,
	reference_docname: str | None = None,
):
	# The parsed event only: ids, statuses and amounts, never the delivered payload.
	data = {"gateway": gateway, **(event.model_dump(exclude_none=True) if event else {})}
	return create_request_log(
		data,
		service_name=f"{gateway} Webhook",
		status=status,
		reference_doctype="Gateway Payment Request",
		reference_docname=reference_docname,
		request_id=event.event_id if event else None,
	)
