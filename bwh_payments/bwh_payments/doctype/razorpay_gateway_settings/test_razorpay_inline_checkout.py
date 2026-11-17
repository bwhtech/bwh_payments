# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import get_url
from pydantic import ValidationError

from bwh_payments.api import create_payment
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RazorpayTestCase,
	make_razorpay_payment_request,
)
from bwh_payments.services.razorpay.stub import (
	StubRazorpayClient,
	build_order_paid_event,
	sign_inline_payment,
	sign_payload,
)
from bwh_payments.tests.test_razorpay_webhook import RAZORPAY_WEBHOOK_SECRET


def make_inline_payment_request(amount: float = 100):
	return create_payment("Currency", "INR", amount, "INR", RAZORPAY_GATEWAY, inline_sdks=("razorpay",))


class TestRazorpayInlineCheckout(RazorpayTestCase):
	def test_an_inline_payment_creates_an_order_in_minor_units_with_the_request_as_receipt(self):
		payment_request = make_inline_payment_request(1234.56)

		order = StubRazorpayClient.created_orders[-1]
		self.assertEqual((order["amount"], order["currency"]), (123456, "INR"))
		self.assertEqual(order["receipt"], payment_request.name)
		self.assertTrue(payment_request.order_ref.startswith("order_"))
		self.assertFalse(payment_request.order_url)

	def test_the_checkout_options_carry_the_order_and_no_secrets(self):
		payment_request = make_inline_payment_request(100)

		checkout = payment_request.get_inline_checkout()

		self.assertEqual((checkout["request"], checkout["sdk"]), (payment_request.name, "razorpay"))
		self.assertEqual(checkout["script_url"], "https://checkout.razorpay.com/v1/checkout.js")
		options = checkout["options"]
		self.assertEqual(options["key"], "rzp_test_x")
		self.assertEqual(options["order_id"], payment_request.order_ref)
		self.assertEqual((options["amount"], options["currency"]), (10000, "INR"))
		self.assertIn("prefill", options)
		self.assertNotIn("rzp_test_secret", frappe.as_json(checkout))

	def get_options(self, display: dict | None = None) -> dict:
		return make_inline_payment_request(100).get_inline_checkout(display)["options"]

	def test_display_values_reach_the_options(self):
		options = self.get_options(
			{
				"name": "Buzz Events",
				"description": "Two tickets",
				"timeout": 600,
				"lock_customer": True,
				"notes": {"booking": "B-1"},
			}
		)

		self.assertEqual((options["name"], options["description"]), ("Buzz Events", "Two tickets"))
		self.assertEqual(options["timeout"], 600)
		self.assertEqual(options["readonly"], {"email": True, "name": True})
		self.assertEqual(options["notes"]["booking"], "B-1")

	def test_without_display_only_the_defaults_remain(self):
		options = self.get_options()

		for key in ("name", "image", "timeout", "readonly"):
			self.assertNotIn(key, options)
		self.assertEqual(options["description"], "Online order payment")
		self.assertEqual(options["modal"], {"confirm_close": True})

	def test_a_relative_image_becomes_absolute(self):
		options = self.get_options({"image": "/files/logo.png"})

		self.assertEqual(options["image"], get_url("/files/logo.png"))

	def test_the_callers_notes_join_the_reference(self):
		options = self.get_options({"notes": {"booking": "B-1"}})

		self.assertEqual(options["notes"]["booking"], "B-1")
		self.assertIn("reference_id", options["notes"])

	def test_an_unknown_display_key_is_rejected(self):
		with self.assertRaises(ValidationError):
			self.get_options({"colour": "red"})

	def test_an_autoincrement_name_in_the_notes_is_sent_as_text(self):
		options = self.get_options({"notes": {"event": 31246}})

		self.assertEqual(options["notes"]["event"], "31246")

	def test_a_frappe_phone_number_is_prefilled_without_the_hyphen(self):
		from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.inline_checkout import (
			get_customer_details,
		)

		self.assertEqual(get_customer_details({"phone": "+91-9876543210"})["contact"], "+919876543210")

	def test_a_paid_request_has_no_inline_checkout(self):
		payment_request = make_inline_payment_request(100)
		StubRazorpayClient.pay_order(payment_request.order_ref)
		payment_request.sync_status()

		self.assertIsNone(payment_request.get_inline_checkout())

	def test_the_checkout_carries_where_the_shopper_goes_after_paying(self):
		payment_request = create_payment(
			"Currency",
			"INR",
			100,
			"INR",
			RAZORPAY_GATEWAY,
			inline_sdks=("razorpay",),
			redirect_to="/orders/1",
		)

		self.assertEqual(payment_request.get_inline_checkout()["redirect_to"], "/orders/1")

	def test_an_order_cannot_be_cancelled_and_razorpay_is_not_called(self):
		with patch.object(StubRazorpayClient, "cancel_payment_link") as cancel_payment_link:
			self.assertFalse(self.get_settings().cancel_session("order_x"))

		cancel_payment_link.assert_not_called()

	def test_a_link_payment_has_no_inline_checkout(self):
		payment_request = make_razorpay_payment_request(100)

		self.assertIsNone(payment_request.get_inline_checkout())

	def test_a_paid_order_is_paid(self):
		order_id = StubRazorpayClient.add_order()
		StubRazorpayClient.pay_order(order_id)

		self.assertEqual(self.get_settings().get_payment_status(order_id), "Paid")

	def test_a_created_order_is_pending(self):
		order_id = StubRazorpayClient.add_order()

		self.assertEqual(self.get_settings().get_payment_status(order_id), "Pending")

	def test_an_authorised_order_payment_stays_pending_by_default(self):
		order_id = StubRazorpayClient.add_order()
		StubRazorpayClient.add_order_payment(order_id, "authorized")

		self.assertEqual(self.get_settings().get_payment_status(order_id), "Pending")

	def test_an_authorised_order_payment_counts_as_paid_when_authorised_is_treated_as_paid(self):
		frappe.db.set_single_value("Razorpay Gateway Settings", "treat_authorised_as_paid", 1)
		frappe.clear_cache(doctype="Razorpay Gateway Settings")
		order_id = StubRazorpayClient.add_order()
		StubRazorpayClient.add_order_payment(order_id, "authorized")

		self.assertEqual(self.get_settings().get_payment_status(order_id), "Paid")

	def test_a_paid_order_is_traced_to_its_captured_payment(self):
		payment_request = make_inline_payment_request(100)
		payment_id = StubRazorpayClient.pay_order(payment_request.order_ref)

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.gateway_transaction_ref, payment_id)

	def test_a_refund_goes_to_the_captured_payment_of_the_order(self):
		order_id = StubRazorpayClient.add_order()
		StubRazorpayClient.add_order_payment(order_id, "failed")
		payment_id = StubRazorpayClient.pay_order(order_id)

		self.get_settings().refund_payment(order_id, 40, "INR")

		self.assertEqual(StubRazorpayClient.created_refunds[-1], {"payment_id": payment_id, "amount": 4000})


class TestRazorpayInlineWebhook(RazorpayTestCase):
	def handle(self, payload: bytes):
		headers = {
			"X-Razorpay-Signature": sign_payload(payload, RAZORPAY_WEBHOOK_SECRET),
			"X-Razorpay-Event-Id": "evt_order_1",
		}
		return self.get_settings().handle_webhook(payload, headers)

	def test_an_order_paid_event_is_mapped_to_the_order_id_and_status(self):
		result = self.handle(build_order_paid_event("order_abc"))

		self.assertEqual((result.session_id, result.status), ("order_abc", "Paid"))

	def test_an_order_paid_event_without_a_paid_order_throws_so_razorpay_retries(self):
		with self.assertRaises(frappe.ValidationError):
			self.handle(build_order_paid_event("order_abc", status="attempted"))

	def test_an_order_paid_event_without_an_order_throws_so_razorpay_retries(self):
		payload = b'{"event": "order.paid", "payload": {}}'

		with self.assertRaises(frappe.ValidationError):
			self.handle(payload)


class TestRazorpayInlineVerification(RazorpayTestCase):
	def verify(self, response: dict, order_id: str = "order_1") -> bool:
		return self.get_settings().verify_inline_payment(order_id, response)

	def sign_response(self, order_id: str = "order_1", payment_id: str = "pay_1") -> dict:
		return {
			"razorpay_order_id": order_id,
			"razorpay_payment_id": payment_id,
			"razorpay_signature": sign_inline_payment(order_id, payment_id, "rzp_test_secret"),
		}

	def test_a_correctly_signed_response_is_accepted(self):
		self.assertTrue(self.verify(self.sign_response()))

	def test_a_wrong_signature_is_rejected(self):
		response = {**self.sign_response(), "razorpay_signature": "0" * 64}

		self.assertFalse(self.verify(response))

	def test_a_response_signed_with_another_secret_is_rejected(self):
		response = self.sign_response()
		response["razorpay_signature"] = sign_inline_payment("order_1", "pay_1", "attacker_guess")

		self.assertFalse(self.verify(response))

	def test_a_response_for_another_order_is_rejected(self):
		self.assertFalse(self.verify(self.sign_response(order_id="order_2")))

	def test_a_payment_id_swapped_after_signing_is_rejected(self):
		response = {**self.sign_response(), "razorpay_payment_id": "pay_2"}

		self.assertFalse(self.verify(response))

	def test_a_response_missing_fields_is_rejected(self):
		self.assertFalse(self.verify({}))
		self.assertFalse(self.verify({"razorpay_order_id": "order_1"}))
		self.assertFalse(self.verify({"razorpay_order_id": "order_1", "razorpay_payment_id": "pay_1"}))
