# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.api import get_gateways
from bwh_payments.services.mpesa.stub import StubMpesaClient, build_reversal_result, build_stk_callback

MPESA_GATEWAY = "M-Pesa"
MPESA_CALLBACK_TOKEN = "mpesa_test_callback_token"
SHOPPER_PHONE = "0712 345 678"
SUCCESS_URL = "https://shop.test/en/account/orders/confirmation"
RETURN_URL = (
	"https://shop.test/api/method/bwh_payments.bwh_payments.checkout.return_from_gateway?request=GPR-1"
)

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Address",
	"Company",
	"Customer",
	"Payment Request",
]


def configure_mpesa_gateway():
	if not frappe.db.exists("Currency", "KES"):
		frappe.get_doc({"doctype": "Currency", "currency_name": "KES", "enabled": 1}).insert(
			ignore_permissions=True
		)

	settings = frappe.get_single("Mpesa Gateway Settings")
	settings.update(
		{
			"enabled": 1,
			"sandbox": 1,
			"consumer_key": "mpesa_test_key",
			"consumer_secret": "mpesa_test_secret",
			"business_shortcode": "174379",
			"online_passkey": "mpesa_test_passkey",
			"transaction_type": "Pay Bill",
			"till_number": "",
			"initiator_name": "testapi",
			"security_credential": "mpesa_test_credential",
			"callback_token": MPESA_CALLBACK_TOKEN,
			"webhook_ips": "",
			"success_url": SUCCESS_URL,
			"failure_url": "https://shop.test/en/cart/checkout",
			"cancelled_url": "https://shop.test/en/cart",
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("Payment Gateway Profile", MPESA_GATEWAY):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": MPESA_GATEWAY,
				"gateway_settings": "Mpesa Gateway Settings",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def remove_mpesa_gateway():
	"""`create_request_log` commits, so these tests escape the rollback and need this cleanup."""
	request_names = frappe.get_all("Gateway Payment Request", {"gateway": MPESA_GATEWAY}, pluck="name")
	frappe.db.delete("Gateway Refund", {"payment_request": ["in", request_names]})
	for request_name in request_names:
		frappe.delete_doc(
			"Gateway Payment Request", request_name, ignore_permissions=True, delete_permanently=True
		)

	for service in ("M-Pesa", f"{MPESA_GATEWAY} Webhook"):
		for log_name in frappe.get_all(
			"Integration Request", filters={"integration_request_service": service}, pluck="name"
		):
			frappe.delete_doc(
				"Integration Request", log_name, ignore_permissions=True, delete_permanently=True
			)

	frappe.delete_doc(
		"Payment Gateway Profile",
		MPESA_GATEWAY,
		ignore_missing=True,
		ignore_permissions=True,
		force=True,
	)
	# Not save(): the keys are mandatory.
	frappe.db.set_single_value(
		"Mpesa Gateway Settings",
		{
			"enabled": 0,
			"consumer_key": "",
			"consumer_secret": "",
			"business_shortcode": "",
			"online_passkey": "",
			"initiator_name": "",
			"security_credential": "",
			"callback_token": "",
			"success_url": "",
			"failure_url": "",
			"cancelled_url": "",
		},
	)
	frappe.clear_cache(doctype="Mpesa Gateway Settings")


def make_mpesa_payment_request(amount: float = 100, phone: str = SHOPPER_PHONE):
	return frappe.get_doc(
		{
			"doctype": "Gateway Payment Request",
			"gateway": MPESA_GATEWAY,
			"amount": amount,
			"currency_code": "KES",
			"ref_doctype": "Currency",
			"ref_docname": "KES",
			"customer_phone": phone,
		}
	).insert(ignore_permissions=True)


def set_callback_request(token: str | None = MPESA_CALLBACK_TOKEN, payload: bytes = b""):
	"""Daraja's callback URL carries the gateway and the token; `handle_webhook` reads the token from it."""
	query = {"gateway": MPESA_GATEWAY}
	if token is not None:
		query["token"] = token
	builder = EnvironBuilder(
		method="POST",
		path="/api/method/bwh_payments.bwh_payments.webhook.handle",
		query_string=query,
		data=payload,
		headers={"Content-Type": "application/json"},
	)
	frappe.local.request = Request(builder.get_environ())
	# Normally set by the WSGI handler; the rate limiter counts against it.
	frappe.local.request_ip = "127.0.0.1"


class MpesaTestCase(IntegrationTestCase):
	def setUp(self):
		StubMpesaClient.reset()
		configure_mpesa_gateway()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	def restore_request(self):
		frappe.local.request = self.original_request

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Commit only the cleanup; frappe's class-level rollback would undo it otherwise.
		frappe.db.rollback()
		remove_mpesa_gateway()
		frappe.db.commit()  # nosemgrep

	def get_settings(self):
		return frappe.get_single("Mpesa Gateway Settings")


class TestMpesaGatewaySettings(MpesaTestCase):
	def test_create_session_sends_an_stk_push_to_the_shoppers_phone(self):
		session = self.get_settings().create_session(
			1500, "KES", reference="GPR-0001", customer={"phone": "+254 712 345 678"}
		)

		push = StubMpesaClient.created_pushes[-1]
		self.assertEqual(push["Amount"], 1500)
		self.assertEqual((push["PartyA"], push["PhoneNumber"]), (254712345678, 254712345678))
		self.assertEqual(push["TransactionType"], "CustomerPayBillOnline")
		self.assertEqual(push["PartyB"], "174379")
		self.assertEqual(push["AccountReference"], "GPR-0001")
		self.assertTrue(session.session_id.startswith("ws_CO_"))

	def test_create_session_never_sends_credentials_in_the_payload(self):
		self.get_settings().create_session(
			100, "KES", reference="GPR-0002", customer={"phone": SHOPPER_PHONE}
		)

		push = StubMpesaClient.created_pushes[-1]
		self.assertNotIn("Password", push)
		self.assertNotIn("BusinessShortCode", push)

	def test_a_buy_goods_store_pays_into_its_till(self):
		settings = self.get_settings()
		settings.transaction_type = "Buy Goods"
		settings.till_number = "600100"

		settings.create_session(100, "KES", reference="GPR-0003", customer={"phone": SHOPPER_PHONE})

		push = StubMpesaClient.created_pushes[-1]
		self.assertEqual(push["TransactionType"], "CustomerBuyGoodsOnline")
		self.assertEqual(push["PartyB"], "600100")

	def test_the_callback_url_names_the_profile_and_carries_the_token(self):
		self.get_settings().create_session(
			100, "KES", reference="GPR-0004", customer={"phone": SHOPPER_PHONE}
		)

		callback_url = urlsplit(StubMpesaClient.created_pushes[-1]["CallBackURL"])
		self.assertEqual(callback_url.path, "/api/method/bwh_payments.bwh_payments.webhook.handle")
		self.assertEqual(
			parse_qs(callback_url.query), {"gateway": [MPESA_GATEWAY], "token": [MPESA_CALLBACK_TOKEN]}
		)

	def test_a_long_reference_is_cut_to_what_daraja_accepts(self):
		self.get_settings().create_session(
			100, "KES", reference="ORDER-2026-000123", customer={"phone": SHOPPER_PHONE}
		)

		self.assertEqual(StubMpesaClient.created_pushes[-1]["AccountReference"], "-2026-000123")

	def test_the_shopper_is_sent_straight_to_the_return_url(self):
		session = self.get_settings().create_session(
			100, "KES", reference="GPR-0005", customer={"phone": SHOPPER_PHONE}, return_url=RETURN_URL
		)

		self.assertEqual(
			(session.redirect_url, session.success_url, session.cancel_url, session.failure_url),
			(RETURN_URL, RETURN_URL, RETURN_URL, RETURN_URL),
		)

	def test_without_a_return_url_the_shopper_goes_to_the_success_url(self):
		session = self.get_settings().create_session(
			100, "KES", reference="GPR-0006", customer={"phone": SHOPPER_PHONE}
		)

		self.assertEqual(session.redirect_url, f"{SUCCESS_URL}?reference_id=GPR-0006")

	def test_kenyan_numbers_are_read_in_every_common_form(self):
		settings = self.get_settings()
		for phone in ("0712345678", "712345678", "254712345678", "+254-712-345-678", "0112345678"):
			settings.create_session(100, "KES", reference="GPR-0007", customer={"phone": phone})

		self.assertEqual(
			[push["PhoneNumber"] for push in StubMpesaClient.created_pushes],
			[254712345678, 254712345678, 254712345678, 254712345678, 254112345678],
		)

	def test_a_missing_or_foreign_phone_number_is_refused_before_any_push(self):
		settings = self.get_settings()
		for customer in ({}, {"phone": ""}, {"phone": "+1 415 555 0100"}, {"phone": "0812345678"}):
			with self.assertRaises(frappe.ValidationError):
				settings.create_session(100, "KES", reference="GPR-0008", customer=customer)

		self.assertEqual(StubMpesaClient.created_pushes, [])

	def test_cents_are_refused_rather_than_rounded(self):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings().create_session(
				100.5, "KES", reference="GPR-0009", customer={"phone": SHOPPER_PHONE}
			)

		self.assertEqual(StubMpesaClient.created_pushes, [])

	def test_a_currency_other_than_kes_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings().create_session(
				100, "USD", reference="GPR-0010", customer={"phone": SHOPPER_PHONE}
			)

		self.assertEqual(self.get_settings().supported_currencies(), ("KES",))

	def test_a_session_needs_an_enabled_profile_to_call_back_to(self):
		frappe.db.set_value("Payment Gateway Profile", MPESA_GATEWAY, "enabled", 0)
		self.addCleanup(frappe.db.set_value, "Payment Gateway Profile", MPESA_GATEWAY, "enabled", 1)

		with self.assertRaises(frappe.ValidationError):
			self.get_settings().create_session(
				100, "KES", reference="GPR-0011", customer={"phone": SHOPPER_PHONE}
			)

	def test_saving_without_a_callback_token_generates_one(self):
		settings = self.get_settings()
		settings.callback_token = ""
		settings.save(ignore_permissions=True)

		token = settings.get_password("callback_token")
		self.assertEqual(len(token), 32)
		self.assertNotEqual(token, MPESA_CALLBACK_TOKEN)

	def get_payment_status(self, result_code):
		return self.get_settings().get_payment_status(StubMpesaClient.add_push(result_code))

	def test_an_unanswered_prompt_is_pending(self):
		self.assertEqual(self.get_payment_status(None), "Pending")

	def test_a_paid_prompt_is_paid(self):
		self.assertEqual(self.get_payment_status("0"), "Paid")

	def test_a_dismissed_prompt_is_cancelled(self):
		self.assertEqual(self.get_payment_status("1032"), "Cancelled")

	def test_a_prompt_the_phone_never_answered_is_expired(self):
		self.assertEqual(self.get_payment_status("1037"), "Expired")

	def test_insufficient_balance_and_a_wrong_pin_are_not_paid(self):
		self.assertEqual(self.get_payment_status("1"), "Not Paid")
		self.assertEqual(self.get_payment_status("2001"), "Not Paid")

	def test_a_number_that_cannot_pay_is_not_paid(self):
		self.assertEqual(self.get_payment_status("11"), "Not Paid")

	def test_an_unrecognised_result_code_stays_pending(self):
		self.assertEqual(self.get_payment_status("4242"), "Pending")
		self.assertEqual(self.get_payment_status("4999"), "Pending")
		self.assertEqual(self.get_payment_status(""), "Pending")

	def test_a_pull_settles_the_request_without_a_receipt(self):
		payment_request = make_mpesa_payment_request()
		StubMpesaClient.pay(payment_request.order_ref)

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Paid")
		self.assertIsNone(payment_request.gateway_transaction_ref)

	def make_paid_request(self, receipt: str = "SAMPLE0001"):
		payment_request = make_mpesa_payment_request()
		StubMpesaClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		payment_request.db_set("gateway_transaction_ref", receipt)
		return payment_request

	def test_a_refund_reverses_the_receipt_in_whole_shillings(self):
		payment_request = self.make_paid_request("SRECEIPT01")

		refund = payment_request.create_refund(40)

		reversal = StubMpesaClient.created_reversals[-1]
		self.assertEqual(reversal["TransactionID"], "SRECEIPT01")
		self.assertEqual(reversal["Amount"], 40)
		self.assertEqual(reversal["CommandID"], "TransactionReversal")
		self.assertEqual(reversal["ReceiverParty"], "174379")
		self.assertIn(f"token={MPESA_CALLBACK_TOKEN}", reversal["ResultURL"])
		self.assertEqual(refund.status, "Pending")
		self.assertTrue(refund.refund_id.startswith("AG_"))

	def test_a_payment_without_a_receipt_cannot_be_reversed(self):
		payment_request = make_mpesa_payment_request()
		StubMpesaClient.pay(payment_request.order_ref)
		payment_request.sync_status()

		with self.assertRaises(frappe.ValidationError):
			payment_request.create_refund(40)

		self.assertEqual(StubMpesaClient.created_reversals, [])

	def test_a_refund_in_cents_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings().refund_payment(self.make_paid_request().order_ref, 10.5, "KES")

		self.assertEqual(StubMpesaClient.created_reversals, [])

	def test_a_security_credential_as_long_as_daraja_issues_is_kept(self):
		settings = self.get_settings()
		settings.security_credential = "x" * 344
		settings.save(ignore_permissions=True)

		self.assertEqual(settings.get_password("security_credential"), "x" * 344)

	def test_mpesa_is_listed_without_a_reusable_checkout(self):
		gateway = next(gateway for gateway in get_gateways() if gateway.name == MPESA_GATEWAY)

		self.assertEqual(gateway.settings_doctype, "Mpesa Gateway Settings")
		self.assertTrue(gateway.description)
		self.assertTrue(gateway.docs_url.startswith("https://"))
		self.assertFalse(gateway.reusable_checkout)


class TestMpesaCallbackVerification(MpesaTestCase):
	def handle(self, payload: bytes, token: str | None = MPESA_CALLBACK_TOKEN):
		set_callback_request(token, payload)
		return self.get_settings().handle_webhook(payload, {"Content-Type": "application/json"})

	def test_a_paid_callback_is_mapped_to_the_checkout_request_and_carries_the_receipt(self):
		result = self.handle(build_stk_callback("ws_CO_paid", receipt="SRECEIPT02"))

		self.assertEqual(result.session_id, "ws_CO_paid")
		self.assertEqual(result.status, "Paid")
		self.assertEqual(result.transaction_ref, "SRECEIPT02")
		self.assertEqual(result.event_id, "ws_CO_paid")

	def test_a_cancelled_callback_is_cancelled(self):
		result = self.handle(build_stk_callback("ws_CO_cancelled", result_code=1032))

		self.assertEqual(result.status, "Cancelled")
		self.assertIsNone(result.transaction_ref)

	def test_a_callback_with_an_unrecognised_result_code_is_not_paid(self):
		result = self.handle(build_stk_callback("ws_CO_unlisted", result_code=4242))

		self.assertEqual(result.status, "Not Paid")

	def test_a_wrong_token_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.handle(build_stk_callback("ws_CO_forged"), token="attacker_guess")

	def test_a_missing_token_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.handle(build_stk_callback("ws_CO_untokened"), token=None)

	def test_a_non_ascii_token_is_rejected_rather_than_crashing(self):
		with self.assertRaises(frappe.ValidationError):
			self.handle(build_stk_callback("ws_CO_unicode"), token="tökén")

	def test_a_callback_from_an_address_outside_the_allowlist_is_rejected(self):
		frappe.db.set_single_value("Mpesa Gateway Settings", "webhook_ips", "196.201.214.200")
		frappe.clear_cache(doctype="Mpesa Gateway Settings")

		with self.assertRaises(frappe.ValidationError):
			self.handle(build_stk_callback("ws_CO_wrong_ip"))

	def test_a_paid_callback_without_a_receipt_is_rejected(self):
		payload = build_stk_callback("ws_CO_no_receipt")
		body = frappe.parse_json(payload.decode())
		body["Body"]["stkCallback"]["CallbackMetadata"]["Item"] = []

		with self.assertRaises(frappe.ValidationError):
			self.handle(frappe.as_json(body).encode())

	def test_a_reversal_result_is_a_refund_event(self):
		result = self.handle(build_reversal_result("AG_reversed"))

		self.assertEqual((result.refund_id, result.status), ("AG_reversed", "processed"))

	def test_a_failed_reversal_result_is_a_failed_refund_event(self):
		result = self.handle(build_reversal_result("AG_refused", result_code=2001))

		self.assertEqual((result.refund_id, result.status), ("AG_refused", "failed"))

	def test_an_unknown_body_is_ignored(self):
		self.assertIsNone(self.handle(b'{"something": "else"}'))
		self.assertIsNone(self.handle(b"not json"))
