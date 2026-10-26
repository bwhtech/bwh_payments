# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch
from urllib.parse import urlencode

import braintree
import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.braintree_gateway_settings.braintree_gateway_settings import (
	pay,
	start_checkout,
)
from bwh_payments.bwh_payments.doctype.braintree_gateway_settings.test_braintree_gateway_settings import (
	BRAINTREE_GATEWAY,
	BraintreeTestCase,
	make_braintree_payment_request,
)
from bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request import (
	GatewayPaymentRequest,
)
from bwh_payments.services.braintree.stub import NO_THREE_D_SECURE_NONCE, VALID_NONCE, StubBraintreeClient


def disable_profile(test):
	frappe.db.set_value("Payment Gateway Profile", BRAINTREE_GATEWAY, "enabled", 0)
	test.addCleanup(frappe.db.set_value, "Payment Gateway Profile", BRAINTREE_GATEWAY, "enabled", 1)


def keep_profile_for_history_only(test):
	frappe.db.set_value("Payment Gateway Profile", BRAINTREE_GATEWAY, "gateway_settings", None)
	test.addCleanup(
		frappe.db.set_value,
		"Payment Gateway Profile",
		BRAINTREE_GATEWAY,
		"gateway_settings",
		"Braintree Gateway Settings",
	)


class TestBraintreePay(BraintreeTestCase):
	def test_a_successful_sale_marks_the_request_paid_without_the_return_page(self):
		payment_request = make_braintree_payment_request(49.99)

		result = pay(payment_request.order_ref, VALID_NONCE)

		self.assertEqual(result["redirect_url"], payment_request.success_url)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertTrue(payment_request.gateway_transaction_ref)

	def test_a_failed_order_after_a_sale_keeps_the_charge_and_still_redirects(self):
		payment_request = make_braintree_payment_request(49.99)
		order_failure = frappe.LinkValidationError("Could not find Mode of Payment: Braintree")

		with patch.object(GatewayPaymentRequest, "on_update", side_effect=order_failure):
			result = pay(payment_request.order_ref, VALID_NONCE)

		self.assertEqual(result["redirect_url"], payment_request.success_url)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")
		self.assertTrue(payment_request.gateway_transaction_ref)

		payment_request.sync_status()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(len(StubBraintreeClient.sales), 1)

	def test_the_order_is_placed_as_the_shopper(self):
		payment_request = make_braintree_payment_request(20)
		placed_by = []
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Guest")

		with patch.object(
			GatewayPaymentRequest, "on_update", side_effect=lambda: placed_by.append(frappe.session.user)
		):
			pay(payment_request.order_ref, VALID_NONCE)

		# Placed as Administrator, the storefront stamps Administrator as owner and hides the order.
		self.assertEqual(placed_by, ["Guest"])

	def test_the_amount_comes_from_the_request(self):
		payment_request = make_braintree_payment_request(49.99)

		pay(payment_request.order_ref, VALID_NONCE)

		sale = StubBraintreeClient.sales[0]
		self.assertEqual(sale["amount"], "49.99")
		self.assertEqual(sale["order_id"], payment_request.order_ref)

	def test_a_second_pay_does_not_charge_twice(self):
		payment_request = make_braintree_payment_request(20)

		pay(payment_request.order_ref, VALID_NONCE)
		second = pay(payment_request.order_ref, VALID_NONCE)

		self.assertEqual(len(StubBraintreeClient.sales), 1)
		self.assertEqual(second["redirect_url"], payment_request.success_url)

	def test_a_sale_the_page_missed_is_not_charged_again(self):
		payment_request = make_braintree_payment_request(20)
		StubBraintreeClient.add_transaction(payment_request.order_ref, amount="20.00")

		pay(payment_request.order_ref, VALID_NONCE)

		self.assertEqual(StubBraintreeClient.sales, [])
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_decline_keeps_the_request_open_for_another_card(self):
		payment_request = make_braintree_payment_request(2000)

		result = pay(payment_request.order_ref, VALID_NONCE)

		self.assertIn("Do Not Honor", result["error"])
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_bad_nonce_is_a_retryable_error(self):
		payment_request = make_braintree_payment_request(20)

		result = pay(payment_request.order_ref, "tampered-nonce")

		self.assertIn("error", result)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_cancelled_request_is_refused(self):
		payment_request = make_braintree_payment_request(20)
		payment_request.release_if_unpaid()

		with self.assertRaises(frappe.ValidationError):
			pay(payment_request.order_ref, VALID_NONCE)
		self.assertEqual(StubBraintreeClient.sales, [])

	def test_an_unknown_session_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			pay("not-a-session", VALID_NONCE)
		self.assertEqual(StubBraintreeClient.sales, [])

	def test_the_request_name_is_not_a_session(self):
		payment_request = make_braintree_payment_request(20)

		with self.assertRaises(frappe.PermissionError):
			pay(payment_request.name, VALID_NONCE)

	def test_a_card_without_three_d_secure_is_refused_when_it_is_switched_on(self):
		payment_request = make_braintree_payment_request(20)

		result = pay(payment_request.order_ref, NO_THREE_D_SECURE_NONCE)

		self.assertIn("three_d_secure", result["error"])
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")
		self.assertFalse(payment_request.gateway_transaction_ref)

	def test_a_card_without_three_d_secure_is_charged_when_it_is_switched_off(self):
		settings = self.get_settings()
		settings.three_d_secure = 0
		settings.save(ignore_permissions=True)
		payment_request = make_braintree_payment_request(20)

		pay(payment_request.order_ref, NO_THREE_D_SECURE_NONCE)

		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_disabled_profile_is_refused(self):
		payment_request = make_braintree_payment_request(20)
		disable_profile(self)

		with self.assertRaises(frappe.PermissionError):
			pay(payment_request.order_ref, VALID_NONCE)
		self.assertEqual(StubBraintreeClient.sales, [])

	def test_a_profile_kept_for_history_only_is_refused(self):
		payment_request = make_braintree_payment_request(20)
		keep_profile_for_history_only(self)

		with self.assertRaises(frappe.PermissionError):
			pay(payment_request.order_ref, VALID_NONCE)
		self.assertEqual(StubBraintreeClient.sales, [])


class TestBraintreeCheckoutPage(BraintreeTestCase):
	def test_an_open_request_renders_drop_in_with_the_request_amount(self):
		payment_request = make_braintree_payment_request(49.99)

		response = start_checkout(payment_request.order_ref)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.headers["Cache-Control"], "no-store")
		page = response.get_data(as_text=True)
		self.assertIn("stub-client-token-usd_account", page)
		self.assertIn("49.99 USD", page)
		self.assertIn(payment_request.order_ref, page)

	def test_a_paid_request_redirects_to_its_success_url(self):
		payment_request = make_braintree_payment_request(20)
		pay(payment_request.order_ref, VALID_NONCE)

		response = start_checkout(payment_request.order_ref)

		self.assertEqual(response.status_code, 302)
		self.assertEqual(response.headers["Location"], payment_request.success_url)

	def test_a_closed_request_shows_no_card_form(self):
		payment_request = make_braintree_payment_request(20)
		payment_request.release_if_unpaid()

		page = start_checkout(payment_request.order_ref).get_data(as_text=True)

		self.assertIn("This payment is no longer open.", page)
		self.assertNotIn("dropin", page)

	def test_an_unknown_session_shows_an_invalid_link(self):
		response = start_checkout("not-a-session")

		self.assertEqual(response.status_code, 404)
		self.assertIn("This payment link is not valid.", response.get_data(as_text=True))

	def test_a_disabled_profile_shows_an_invalid_link(self):
		payment_request = make_braintree_payment_request(20)
		disable_profile(self)

		response = start_checkout(payment_request.order_ref)

		self.assertEqual(response.status_code, 404)
		self.assertEqual(StubBraintreeClient.client_tokens, [])

	def test_a_profile_kept_for_history_only_shows_an_invalid_link(self):
		payment_request = make_braintree_payment_request(20)
		keep_profile_for_history_only(self)

		response = start_checkout(payment_request.order_ref)

		self.assertEqual(response.status_code, 404)
		self.assertEqual(StubBraintreeClient.client_tokens, [])


class TestBraintreeWebhookSpine(BraintreeTestCase):
	def setUp(self):
		super().setUp()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	def restore_request(self):
		frappe.local.request = self.original_request

	def post_webhook(self, form: dict):
		builder = EnvironBuilder(
			method="POST",
			path="/api/method/bwh_payments.bwh_payments.webhook.handle",
			data=urlencode(form).encode(),
		)
		builder.headers.extend({"Content-Type": "application/x-www-form-urlencoded"})
		environ = builder.get_environ()
		environ["QUERY_STRING"] = f"gateway={BRAINTREE_GATEWAY}"
		frappe.local.request = Request(environ)
		# Normally set by the WSGI handler; the rate limiter counts against it.
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def sample_notification(self) -> dict:
		# Signed locally with the configured keys, the way Braintree signs a delivery.
		gateway = self.get_settings().get_braintree_gateway()
		return gateway.webhook_testing.sample_notification(braintree.WebhookNotification.Kind.Check, "check")

	def test_a_signed_notification_is_accepted_and_changes_nothing(self):
		payment_request = make_braintree_payment_request(20)

		response = self.post_webhook(self.sample_notification())

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_a_forged_signature_is_rejected(self):
		form = self.sample_notification()
		form["bt_signature"] = "test_public_key|0000000000000000000000000000000000000000"

		response = self.post_webhook(form)

		self.assertEqual(response["status"], "error")
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)

	def test_a_delivery_without_a_signature_is_rejected(self):
		form = self.sample_notification()
		del form["bt_signature"]

		response = self.post_webhook(form)

		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		self.assertEqual(response["status"], "error")
