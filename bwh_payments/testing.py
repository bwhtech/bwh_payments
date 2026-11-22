"""Test helpers for apps that take payments through bwh_payments.

They drive an in-memory gateway, so a consumer's tests exercise the real payment request, refund and
webhook code without naming a provider or touching the network. Refunds and webhook logs commit:
wrap the test class in `without_commits()`, or call `remove_test_gateway()` after the class rollback.
"""

import atexit
from contextlib import contextmanager
from functools import cache

import frappe
from frappe.database import get_db
from frappe.query_builder import Field
from frappe.utils import now_datetime
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.utils import run_as_administrator, without_commits
from bwh_payments.currency import to_minor_units
from bwh_payments.services.razorpay.stub import (  # nosemgrep: frappe-monkey-patching-not-allowed
	StubRazorpayClient,
	build_refund_event,
	sign_checkout_payment,
	sign_payload,
)

TEST_GATEWAY = "Test Gateway"
SETTINGS = "Razorpay Gateway Settings"
WEBHOOK_SECRET = "bwh_payments_test_webhook_secret"
REFUND_STATUSES = {"Pending": "pending", "Processed": "processed", "Failed": "failed"}
# What GET /payments/{id} says for each payment method a caller can ask `pay` for.
PAYMENT_METHOD_RESPONSES = {
	"upi": {"method": "upi"},
	"card_credit": {"method": "card", "card": {"type": "credit"}},
	"card_debit": {"method": "card", "card": {"type": "debit"}},
	"netbanking": {"method": "netbanking"},
	"wallet": {"method": "wallet"},
	"emi": {"method": "emi"},
	"paylater": {"method": "paylater"},
}


def reset():
	"""Forget every payment and refund the in-memory gateway holds. Call in `setUp`."""
	StubRazorpayClient.reset()


def configure_test_gateway(name: str = TEST_GATEWAY) -> str:
	"""An enabled Payment Gateway Profile on the in-memory gateway. Returns its name.

	The site's own Razorpay settings come back when the test process exits.
	"""
	keep_site_settings()
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


@cache
def keep_site_settings():
	rows = {
		name: frappe.qb.from_(name).select("*").where(Field("doctype") == SETTINGS).run(as_dict=True)
		for name in ("Singles", "__Auth")
	}
	atexit.register(restore_site_settings, frappe.local.site, rows)


def restore_site_settings(site: str, rows: dict):
	frappe.init(site)
	frappe.connect()
	try:
		for name, table_rows in rows.items():
			frappe.qb.from_(name).delete().where(Field("doctype") == SETTINGS).run()
			if table_rows:
				frappe.qb.into(name).columns(*table_rows[0]).insert(
					*(tuple(row.values()) for row in table_rows)
				).run()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.clear_document_cache(SETTINGS)
	finally:
		frappe.destroy()


def remove_test_gateway(name: str = TEST_GATEWAY):
	requests = frappe.get_all("Gateway Payment Request", filters={"gateway": name}, pluck="name")
	if requests:
		frappe.db.delete("Gateway Refund", {"payment_request": ["in", requests]})
		frappe.db.delete("Gateway Payment Request", {"name": ["in", requests]})
	frappe.db.delete("Integration Request", {"integration_request_service": f"{name} Webhook"})
	frappe.delete_doc(
		"Payment Gateway Profile", name, ignore_missing=True, ignore_permissions=True, force=True
	)


@contextmanager
def other_connection():
	"""A second database session, as a concurrent worker holds. It sees only what is committed."""
	conf = frappe.conf
	database = get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)
	database.connect()
	try:
		yield database
	finally:
		database.rollback()
		database.close()


def commit_elsewhere(query: str, values: tuple | dict | None = None):
	"""Run `query` in another session and commit it, as a concurrent worker would.

	A plain read in this session keeps its earlier snapshot and does not see the change.
	"""
	with other_connection() as database:
		database.sql(query, values)
		database.commit()


def reserve_refund_elsewhere(payment_request: str, amount: float, reference: str | None = None) -> str:
	"""A Pending refund another worker reserved and committed. Returns its name.

	It outlives the test's rollback, so delete it once this session holds no lock on it.
	"""
	name = frappe.generate_hash(length=10)
	now = now_datetime()
	commit_elsewhere(
		"""insert into `tabGateway Refund` (name, creation, modified, owner, modified_by, docstatus,
		payment_request, amount, reference, status) values (%s, %s, %s, 'Administrator', 'Administrator',
		0, %s, %s, %s, 'Pending')""",
		(name, now, now, payment_request, amount, reference),
	)
	return name


def pay(payment_request: str, payment_method: str = "upi"):
	"""The shopper pays with `payment_method` (a PAYMENT_METHOD_RESPONSES key), and the payment is
	confirmed as it would be on their return."""
	doc = frappe.get_doc("Gateway Payment Request", payment_request)
	StubRazorpayClient.pay(doc.order_ref, PAYMENT_METHOD_RESPONSES[payment_method])
	with run_as_administrator():
		doc.sync_status()


def complete_embedded_checkout(payment_request: str, payment_method: str = "upi") -> dict:
	"""The shopper pays in an embedded checkout. Returns what the checkout script hands the page,
	for `api.confirm_embedded_payment` or the caller's own endpoint that calls it."""
	order_id = frappe.db.get_value("Gateway Payment Request", payment_request, "order_ref")
	payment_id = StubRazorpayClient.pay(order_id, PAYMENT_METHOD_RESPONSES[payment_method])
	key_secret = frappe.get_single(SETTINGS).get_password("key_secret")
	return {
		"razorpay_payment_id": payment_id,
		"razorpay_order_id": order_id,
		"razorpay_signature": sign_checkout_payment(order_id, payment_id, key_secret),
	}


def set_next_refund_status(status: str):
	"""How the gateway answers the next refund: "Pending", "Processed" or "Failed" (refused)."""
	StubRazorpayClient.next_refund_status = REFUND_STATUSES[status]


def set_refund_status(refund: str, status: str):
	"""The gateway now reports this refund as "Processed" or "Failed", for `api.sync_refunds` to read."""
	StubRazorpayClient.set_refund_status(
		frappe.db.get_value("Gateway Refund", refund, "refund_id"), REFUND_STATUSES[status]
	)


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
