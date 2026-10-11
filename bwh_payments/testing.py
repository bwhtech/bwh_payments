"""Test helpers for apps that take payments through bwh_payments.

They drive an in-memory gateway, so a consumer's tests exercise the real payment request, refund and
webhook code without naming a provider or touching the network. Refunds and webhook logs commit, so
call `remove_test_gateway()` in `tearDownClass` after the class rollback.
"""

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.utils import run_as_administrator
from bwh_payments.currency import to_minor_units
from bwh_payments.services.razorpay.stub import StubRazorpayClient, build_refund_event, sign_payload

TEST_GATEWAY = "Test Gateway"
SETTINGS = "Razorpay Gateway Settings"
WEBHOOK_SECRET = "bwh_payments_test_webhook_secret"
REFUND_STATUSES = {"Pending": "pending", "Processed": "processed", "Failed": "failed"}


def reset():
	"""Forget every payment and refund the in-memory gateway holds. Call in `setUp`."""
	StubRazorpayClient.reset()


def configure_test_gateway(name: str = TEST_GATEWAY) -> str:
	"""An enabled Payment Gateway Profile on the in-memory gateway. Returns its name."""
	settings = frappe.get_single(SETTINGS)
	settings.update(
		{
			"key_id": "rzp_test_bwh_payments",
			"key_secret": "rzp_test_bwh_payments_secret",
			"webhook_secret": WEBHOOK_SECRET,
			"currency": "INR",
			"success_url": "/",
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", name):
		frappe.get_doc(
			{"doctype": "Payment Gateway Profile", "name": name, "gateway_settings": SETTINGS, "enabled": 1}
		).insert(ignore_permissions=True)
	return name


def remove_test_gateway(name: str = TEST_GATEWAY):
	requests = frappe.get_all("Gateway Payment Request", filters={"gateway": name}, pluck="name")
	if requests:
		frappe.db.delete("Gateway Refund", {"payment_request": ["in", requests]})
		frappe.db.delete("Gateway Payment Request", {"name": ["in", requests]})
	frappe.db.delete("Integration Request", {"integration_request_service": f"{name} Webhook"})
	frappe.delete_doc(
		"Payment Gateway Profile", name, ignore_missing=True, ignore_permissions=True, force=True
	)


def pay(payment_request: str):
	"""The shopper pays, and the payment is confirmed as it would be on their return."""
	doc = frappe.get_doc("Gateway Payment Request", payment_request)
	StubRazorpayClient.pay(doc.order_ref)
	with run_as_administrator():
		doc.sync_status()


def set_next_refund_status(status: str):
	"""How the gateway answers the next refund: "Pending", "Processed" or "Failed" (refused)."""
	StubRazorpayClient.next_refund_status = REFUND_STATUSES[status]


def send_refund_webhook(refund: str, status: str):
	"""The gateway reports a refund raised through this app as "Processed" or "Failed"."""
	gateway_refund = frappe.get_doc("Gateway Refund", refund)
	payment_request = frappe.get_doc("Gateway Payment Request", gateway_refund.payment_request)
	post_refund_event(
		payment_request,
		gateway_refund.refund_id,
		REFUND_STATUSES[status],
		gateway_refund.amount,
	)


def send_external_refund_webhook(payment_request: str, amount: float, status: str = "Processed") -> str:
	"""The gateway reports a refund raised outside this app, e.g. from its dashboard. Returns its id."""
	refund_id = f"rfnd_{frappe.generate_hash(length=12)}"
	post_refund_event(
		frappe.get_doc("Gateway Payment Request", payment_request), refund_id, REFUND_STATUSES[status], amount
	)
	return refund_id


def post_refund_event(payment_request, refund_id: str, status: str, amount: float):
	payload = build_refund_event(
		refund_id,
		payment_request.gateway_transaction_ref,
		status,
		to_minor_units(amount, payment_request.currency_code),
		payment_request.currency_code,
	)
	post_webhook(
		payment_request.gateway,
		payload,
		{
			"X-Razorpay-Signature": sign_payload(payload, WEBHOOK_SECRET),
			"X-Razorpay-Event-Id": f"evt_{frappe.generate_hash(length=12)}",
		},
	)


def post_webhook(gateway: str, payload: bytes, headers: dict):
	original_request = getattr(frappe.local, "request", None)
	original_response = getattr(frappe.local, "response", None)
	builder = EnvironBuilder(
		method="POST", path="/api/method/bwh_payments.bwh_payments.webhook.handle", data=payload
	)
	builder.headers.extend({"Content-Type": "application/json", **headers})
	environ = builder.get_environ()
	environ["QUERY_STRING"] = f"gateway={gateway}"
	try:
		frappe.local.request = Request(environ)
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		webhook.handle()
		if frappe.local.response.get("http_status_code"):
			raise AssertionError(f"The test webhook was rejected: {frappe.local.response}")
	finally:
		frappe.local.request = original_request
		frappe.local.response = original_response
