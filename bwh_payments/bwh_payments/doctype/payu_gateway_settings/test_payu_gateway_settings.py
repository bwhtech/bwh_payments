# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

import hashlib
import json
from urllib.parse import parse_qsl, urlencode

import frappe
from frappe.auth import CookieManager
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments.doctype.payu_gateway_settings import payu_gateway_settings
from bwh_payments.services.payu.stub import StubPayUClient, build_payment_event, build_refund_event

PAYU_GATEWAY = "PayU"
PAYU_KEY = "payu_test_key"
PAYU_SALT = "payu_test_salt"
SUCCESS_URL = "https://shop.test/en/account/orders/confirmation"
FAILURE_URL = "https://shop.test/en/cart/checkout"
RETURN_URL = (
	"https://shop.test/api/method/bwh_payments.bwh_payments.checkout.return_from_gateway?request=GPR-1"
)
ENDPOINT = "/api/method/bwh_payments.bwh_payments.doctype.payu_gateway_settings.payu_gateway_settings"
CUSTOMER = {"email": "asha@example.com", "phone": "9876543210", "name": {"forenames": "Asha"}}

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_payu_gateway():
	settings = frappe.get_single("PayU Gateway Settings")
	settings.update(
		{
			"enabled": 1,
			"test_mode": 1,
			"merchant_key": PAYU_KEY,
			"merchant_salt": PAYU_SALT,
			"success_url": SUCCESS_URL,
			"failure_url": FAILURE_URL,
			"cancelled_url": "https://shop.test/en/cart",
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", PAYU_GATEWAY):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": PAYU_GATEWAY,
				"gateway_settings": "PayU Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def remove_payu_gateway():
	"""`create_request_log` and `create_refund` commit, so these tests escape the rollback."""
	request_names = frappe.get_all("Gateway Payment Request", {"gateway": PAYU_GATEWAY}, pluck="name")
	frappe.db.delete("Gateway Refund", {"payment_request": ["in", request_names]})
	for request_name in request_names:
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	for service in ("PayU", f"{PAYU_GATEWAY} Webhook"):
		for log_name in frappe.get_all(
			"Integration Request", filters={"integration_request_service": service}, pluck="name"
		):
			frappe.delete_doc(
				"Integration Request", log_name, ignore_permissions=True, delete_permanently=True
			)

	frappe.delete_doc(
		"Payment Gateway Profile", PAYU_GATEWAY, ignore_missing=True, ignore_permissions=True, force=True
	)
	# Not save(): the key and salt are mandatory.
	frappe.db.set_single_value(
		"PayU Gateway Settings",
		{
			"enabled": 0,
			"merchant_key": "",
			"merchant_salt": "",
			"success_url": "",
			"failure_url": "",
			"cancelled_url": "",
		},
	)
	frappe.clear_cache(doctype="PayU Gateway Settings")


def make_payu_payment_request(amount: float = 100, currency: str = "INR", **fields):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": PAYU_GATEWAY,
			"amount": amount,
			"currency_code": currency,
			"ref_doctype": "Currency",
			"ref_docname": currency,
			"customer_email": "asha@example.com",
			"customer_phone": "9876543210",
			"customer_forenames": "Asha",
			**fields,
		}
	).insert(ignore_permissions=True)


class PayUTestCase(IntegrationTestCase):
	def setUp(self):
		StubPayUClient.reset()
		configure_payu_gateway()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	def restore_request(self):
		frappe.local.request = self.original_request

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Commit only the cleanup; frappe's class-level rollback would undo it otherwise.
		frappe.db.rollback()
		remove_payu_gateway()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	def get_settings(self):
		return frappe.get_single("PayU Gateway Settings")

	def fake_request(self, method: str = "GET"):
		frappe.local.request = Request(EnvironBuilder(method=method, path=ENDPOINT).get_environ())
		# Normally set by the WSGI handler; the rate limiter counts against it.
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()

	def make_paid_request(self, amount: float = 100):
		payment_request = make_payu_payment_request(amount)
		StubPayUClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		return payment_request


class TestPayUCheckout(PayUTestCase):
	def test_create_session_returns_a_fresh_txnid_and_our_start_checkout_url(self):
		settings = self.get_settings()

		first = settings.create_session(100, "INR", reference="GPR-1", customer=CUSTOMER)
		second = settings.create_session(100, "INR", reference="GPR-1", customer=CUSTOMER)

		self.assertEqual(len(first.session_id), 20)
		self.assertNotEqual(first.session_id, second.session_id)
		self.assertTrue(first.redirect_url.endswith(f"{ENDPOINT}.start_checkout?request=GPR-1"))

	def test_the_return_urls_carry_the_reference_id(self):
		session = self.get_settings().create_session(100, "INR", reference="GPR-1", customer=CUSTOMER)

		self.assertEqual(session.success_url, f"{SUCCESS_URL}?reference_id=GPR-1")
		self.assertEqual(session.failure_url, f"{FAILURE_URL}?reference_id=GPR-1")
		self.assertIn("reference_id=GPR-1", session.cancel_url)

	def test_every_return_goes_to_the_given_return_url(self):
		session = self.get_settings().create_session(
			100, "INR", reference="GPR-1", customer=CUSTOMER, return_url=RETURN_URL
		)

		self.assertEqual((session.success_url, session.failure_url, session.cancel_url), (RETURN_URL,) * 3)

	def test_a_currency_other_than_inr_is_refused(self):
		self.assertEqual(self.get_settings().supported_currencies(), ("INR",))
		with self.assertRaises(frappe.ValidationError):
			self.get_settings().create_session(100, "USD", reference="GPR-1", customer=CUSTOMER)

	def test_a_shopper_without_email_or_phone_is_refused(self):
		for missing in ("email", "phone"):
			customer = {**CUSTOMER, missing: ""}
			with self.subTest(missing=missing), self.assertRaises(frappe.ValidationError):
				self.get_settings().create_session(100, "INR", reference="GPR-1", customer=customer)

	def get_form(self, payment_request):
		return self.get_settings().build_checkout_form(payment_request)

	def test_the_form_posts_to_the_test_or_live_host(self):
		payment_request = make_payu_payment_request()

		self.assertEqual(self.get_form(payment_request)[0], "https://test.payu.in/_payment")
		frappe.db.set_single_value("PayU Gateway Settings", "test_mode", 0)
		self.assertEqual(self.get_form(payment_request)[0], "https://secure.payu.in/_payment")

	def test_the_form_is_signed_with_the_documented_request_hash(self):
		payment_request = make_payu_payment_request(100)

		fields = self.get_form(payment_request)[1]

		self.assertEqual(fields["amount"], "100.00")
		self.assertEqual(fields["txnid"], payment_request.order_ref)
		self.assertEqual(fields["productinfo"], payment_request.name)
		expected = (
			f"{PAYU_KEY}|{payment_request.order_ref}|100.00|{payment_request.name}|Asha|asha@example.com"
			f"|||||||||||{PAYU_SALT}"
		)
		self.assertEqual(fields["hash"], hashlib.sha512(expected.encode()).hexdigest())

	def test_payu_returns_the_shopper_through_our_bounce_endpoint(self):
		payment_request = make_payu_payment_request()

		fields = self.get_form(payment_request)[1]

		bounce = f"{ENDPOINT}.return_from_payu?request={payment_request.name}&outcome="
		self.assertTrue(fields["surl"].endswith(f"{bounce}success"))
		self.assertTrue(fields["furl"].endswith(f"{bounce}failure"))

	def start_checkout(self, request_name: str):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		self.fake_request()
		return payu_gateway_settings.start_checkout(request_name)

	def test_start_checkout_renders_an_escaped_auto_submitting_form(self):
		payment_request = make_payu_payment_request()
		# Past the save-time sanitiser, as a row written by another app could be.
		frappe.db.set_value(
			"Gateway Payment Request",
			payment_request.name,
			"customer_forenames",
			'"><script>alert(1)</script>',
		)

		body = self.start_checkout(payment_request.name).get_data(as_text=True)

		self.assertIn('action="https://test.payu.in/_payment"', body)
		self.assertIn(payment_request.order_ref, body)
		self.assertNotIn("<script>alert", body)
		self.assertIn("&quot;&gt;&lt;script&gt;", body)

	def test_start_checkout_is_never_cached(self):
		payment_request = make_payu_payment_request()

		response = self.start_checkout(payment_request.name)

		self.assertEqual(response.headers["Cache-Control"], "no-store")

	def test_start_checkout_refuses_a_disabled_gateway(self):
		payment_request = make_payu_payment_request()
		frappe.db.set_value("Payment Gateway Profile", PAYU_GATEWAY, "enabled", 0)
		self.addCleanup(frappe.db.set_value, "Payment Gateway Profile", PAYU_GATEWAY, "enabled", 1)

		with self.assertRaises(frappe.DoesNotExistError):
			self.start_checkout(payment_request.name)

	def test_start_checkout_refuses_a_request_that_is_not_an_open_payu_checkout(self):
		paid = self.make_paid_request()
		elsewhere = make_payu_payment_request()
		frappe.db.set_value("Gateway Payment Request", elsewhere.name, "gateway", "Razorpay")

		for request_name in (paid.name, elsewhere.name, "GPR-never-existed"):
			with self.subTest(request_name=request_name), self.assertRaises(frappe.DoesNotExistError):
				self.start_checkout(request_name)

	def return_from_payu(self, request_name: str, outcome: str):
		self.fake_request("POST")
		frappe.local.cookie_manager = CookieManager()
		frappe.local.cookie_manager.set_cookie("sid", "Guest")
		payu_gateway_settings.return_from_payu(request_name, outcome)
		return frappe.local.response

	def test_the_payu_return_redirects_to_the_stored_url_without_touching_the_session(self):
		payment_request = make_payu_payment_request()

		for outcome, stored in (("success", "success_url"), ("failure", "failure_url")):
			with self.subTest(outcome=outcome):
				response = self.return_from_payu(payment_request.name, outcome)

				self.assertEqual(response.type, "redirect")
				self.assertEqual(response.location, payment_request.get(stored))
				# A cross-site POST carries no cookies; answering with sid=Guest would sign the shopper out.
				self.assertEqual(frappe.local.cookie_manager.cookies, {})

	def test_the_payu_return_refuses_an_unknown_request(self):
		with self.assertRaises(frappe.DoesNotExistError):
			self.return_from_payu("GPR-never-existed", "success")


class TestPayUStatus(PayUTestCase):
	def test_the_status_map(self):
		for unmappedstatus, expected in (
			("captured", "Paid"),
			(" Captured ", "Paid"),
			("userCancelled", "Cancelled"),
			("failed", "Not Paid"),
			("bounced", "Not Paid"),
			("dropped", "Not Paid"),
			("autorefund", "Not Paid"),
			("auth", "Pending"),
			("pending", "Pending"),
			("in progress", "Pending"),
			("who_knows", "Pending"),
		):
			with self.subTest(unmappedstatus=unmappedstatus):
				StubPayUClient.set_status("txn_status", unmappedstatus)
				self.assertEqual(self.get_settings().get_payment_status("txn_status"), expected)

	def test_an_unknown_txnid_is_pending(self):
		self.assertEqual(self.get_settings().get_payment_status("txn_never_seen"), "Pending")

	def test_a_paid_request_is_traced_to_its_mihpayid(self):
		payment_request = make_payu_payment_request()
		mihpayid = StubPayUClient.pay(payment_request.order_ref)

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.gateway_transaction_ref, mihpayid)

	def test_an_open_checkout_cannot_be_released(self):
		payment_request = make_payu_payment_request()

		self.assertFalse(payment_request.release_if_unpaid())
		self.assertEqual(payment_request.status, "Pending")


class TestPayUWebhook(PayUTestCase):
	def handle(self, payload: bytes, content_type: str = "application/x-www-form-urlencoded"):
		return self.get_settings().handle_webhook(payload, {"Content-Type": content_type})

	def test_a_signed_payment_event_is_mapped_to_the_txnid(self):
		mihpayid = StubPayUClient.pay("txn_signed")

		event = self.handle(build_payment_event("txn_signed", PAYU_KEY, PAYU_SALT))

		self.assertEqual(
			(event.session_id, event.status, event.event_id),
			("txn_signed", "Paid", f"txn_signed:{mihpayid}:captured"),
		)

	def test_a_signed_event_with_additional_charges_is_accepted(self):
		payload = build_payment_event("txn_charges", PAYU_KEY, PAYU_SALT, additional_charges="12.50")

		self.assertEqual(self.handle(payload).status, "Paid")

	def test_a_failure_event_is_not_paid(self):
		payload = build_payment_event("txn_failed", PAYU_KEY, PAYU_SALT, unmappedstatus="failed")

		self.assertEqual(self.handle(payload).status, "Not Paid")

	def test_a_forged_or_foreign_event_is_rejected(self):
		forged = dict(parse_qsl(build_payment_event("txn_forged", PAYU_KEY, PAYU_SALT).decode()))
		forged["amount"] = "1.00"
		unsigned = dict(forged)
		del unsigned["hash"]

		for name, payload in (
			("forged", forged),
			("unsigned", unsigned),
			("wrong salt", build_payment_event("txn_forged", PAYU_KEY, "attacker_salt")),
			("foreign key", build_payment_event("txn_forged", "other_key", PAYU_SALT)),
		):
			if isinstance(payload, dict):
				payload = urlencode(payload).encode()
			with self.subTest(name), self.assertRaises(frappe.ValidationError):
				self.handle(payload)

	def test_an_unrelated_body_is_ignored(self):
		self.assertIsNone(self.handle(b'{"hello": "world"}', "application/json"))

	def test_a_refund_event_is_read_back_from_payu_not_from_the_body(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(40)
		StubPayUClient.set_refund_status(refund.refund_id, "SUCCESS")

		event = self.handle(build_refund_event(refund.refund_id, amt="999.00"), "application/json")

		self.assertEqual(
			(event.refund_id, event.status, event.refund_amount, event.transaction_ref, event.event_id),
			(
				refund.refund_id,
				"processed",
				40,
				payment_request.gateway_transaction_ref,
				f"{refund.refund_id}:processed",
			),
		)

	def test_a_settled_refund_payu_does_not_show_yet_is_retried(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(40)

		with self.assertRaises(frappe.ValidationError):
			self.handle(build_refund_event(refund.refund_id, status="success"), "application/json")

	def test_a_numeric_request_id_is_read(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(40)
		StubPayUClient.set_refund_status(refund.refund_id, "SUCCESS")
		body = json.dumps({"request_id": int(refund.refund_id), "status": "success"}).encode()

		self.assertEqual(self.handle(body, "application/json").refund_id, refund.refund_id)

	def test_a_refund_event_for_an_unknown_request_id_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.handle(json.dumps({"request_id": "never_issued", "status": "success"}).encode())


class TestPayURefunds(PayUTestCase):
	def test_a_refund_is_sent_against_the_mihpayid_and_settles_later(self):
		payment_request = self.make_paid_request()

		refund = payment_request.create_refund(40)

		self.assertEqual(StubPayUClient.created_refunds[-1]["var1"], payment_request.gateway_transaction_ref)
		self.assertEqual(StubPayUClient.created_refunds[-1]["var3"], "40.00")
		self.assertLessEqual(len(StubPayUClient.created_refunds[-1]["var2"]), 23)
		self.assertEqual(refund.status, "Pending")

		StubPayUClient.set_refund_status(refund.refund_id, "SUCCESS")
		payment_request.sync_refunds()

		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(flt(payment_request.refund_amount), 40)
		self.assertEqual(payment_request.status, "Partially Refunded")

	def test_the_refund_status_map(self):
		payment_request = self.make_paid_request()
		refund = payment_request.create_refund(40)

		for payu_status, expected in (
			("SUCCESS", "processed"),
			("failure", "failed"),
			("FAILED", "failed"),
			("QUEUED", "pending"),
			("who_knows", "pending"),
		):
			with self.subTest(payu_status=payu_status):
				StubPayUClient.set_refund_status(refund.refund_id, payu_status)
				result = self.get_settings().get_refund_status(payment_request.order_ref, refund.refund_id)
				self.assertEqual(result.status, expected)

	def test_a_refused_refund_is_recorded_as_failed(self):
		payment_request = self.make_paid_request()
		StubPayUClient.refuse_next_refund()

		with self.assertRaises(frappe.ValidationError):
			payment_request.create_refund(40)

		self.assertEqual(
			frappe.get_all("Gateway Refund", {"payment_request": payment_request.name}, pluck="status"),
			["Failed"],
		)

	def test_a_payment_that_is_not_captured_is_not_refunded(self):
		StubPayUClient.set_status("txn_auth", "auth")

		for txnid in ("txn_auth", "txn_never_seen"):
			with self.subTest(txnid=txnid), self.assertRaises(frappe.ValidationError):
				self.get_settings().refund_payment(txnid, 40, "INR")

		self.assertEqual(StubPayUClient.created_refunds, [])
