from unittest.mock import patch

import frappe
from frappe.geo.doctype.currency.currency import Currency

from bwh_payments import testing
from bwh_payments.api import confirm_embedded_payment, create_payment, refund
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	GATEWAY as STRIPE_GATEWAY,
)
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	configure_stripe_gateway,
	remove_stripe_gateway,
)
from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RAZORPAY_GATEWAY,
	RAZORPAY_WEBHOOK_SECRET,
	RazorpayTestCase,
)
from bwh_payments.services.razorpay.stub import (
	StubRazorpayClient,
	build_order_paid_event,
	sign_checkout_payment,
	sign_payload,
)
from bwh_payments.services.stripe.stub import StubStripeClient

CHECKOUT_SCRIPT = "https://checkout.razorpay.com/v1/checkout.js"
# configure_razorpay_gateway's key_secret.
KEY_SECRET = "rzp_test_secret"


class TestEmbeddedCheckout(RazorpayTestCase):
	def create_embedded_payment(self, amount: float = 550, **fields):
		return create_payment(
			"Currency", "INR", amount, "INR", RAZORPAY_GATEWAY, checkout="embedded", **fields
		)

	def checkout_result(self, request, payment_id: str) -> dict:
		return {
			"razorpay_payment_id": payment_id,
			"razorpay_order_id": request.order_ref,
			"razorpay_signature": sign_checkout_payment(request.order_ref, payment_id, KEY_SECRET),
		}

	def test_embedded_checkout_opens_a_razorpay_order(self):
		request = self.create_embedded_payment(550)
		self.assertEqual(request.checkout, "Embedded")
		self.assertTrue(request.order_ref.startswith("order_"))
		self.assertEqual(
			StubRazorpayClient.created_orders,
			[
				{
					"amount": 55000,
					"currency": "INR",
					"receipt": request.name,
					"notes": {"reference_id": request.name},
				}
			],
		)

	def test_embedded_checkout_gives_the_page_razorpay_script_and_options(self):
		self.enterContext(self.change_settings("Website Settings", app_name="Pro Shop"))
		request = self.create_embedded_payment(
			550,
			description="Pro Team via Buzz · PRO-1",
			customer_email="buyer@example.com",
			customer_phone="+919999999999",
		)
		checkout = request.get_embedded_checkout()
		self.assertEqual((checkout.session_id, checkout.script_url), (request.order_ref, CHECKOUT_SCRIPT))
		self.assertEqual(
			checkout.options,
			{
				"key": "rzp_test_x",
				"order_id": request.order_ref,
				"amount": 55000,
				"currency": "INR",
				"name": "Pro Shop",
				"description": "Pro Team via Buzz · PRO-1",
				"prefill": {"email": "buyer@example.com", "contact": "+919999999999"},
			},
		)

	def test_redirect_stays_the_default(self):
		request = create_payment("Currency", "INR", 550, "INR", RAZORPAY_GATEWAY)
		self.assertEqual(request.checkout, "Redirect")
		self.assertTrue(request.order_ref.startswith("plink_"))
		with self.assertRaises(frappe.ValidationError):
			request.get_embedded_checkout()

	def test_an_unknown_checkout_mode_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			create_payment("Currency", "INR", 550, "INR", RAZORPAY_GATEWAY, checkout="popup")

	def test_a_signed_checkout_payment_marks_the_request_paid(self):
		request = self.create_embedded_payment()
		payment_id = StubRazorpayClient.pay(request.order_ref)
		with patch.object(Currency, "on_payment_authorized", create=True) as on_payment_authorized:
			status = confirm_embedded_payment(request.name, self.checkout_result(request, payment_id))
		self.assertEqual(status, "Paid")
		on_payment_authorized.assert_called_once_with("Completed")
		request.reload()
		self.assertEqual((request.status, request.gateway_transaction_ref), ("Paid", payment_id))

	def test_a_forged_signature_is_rejected(self):
		request = self.create_embedded_payment()
		payment_id = StubRazorpayClient.pay(request.order_ref)
		result = self.checkout_result(request, payment_id) | {"razorpay_signature": "0" * 64}
		with self.assertRaises(frappe.ValidationError):
			confirm_embedded_payment(request.name, result)
		self.assertEqual(frappe.db.get_value("Gateway Payment Request", request.name, "status"), "Pending")

	def test_a_signature_for_another_order_is_rejected(self):
		request = self.create_embedded_payment()
		other = self.create_embedded_payment()
		payment_id = StubRazorpayClient.pay(other.order_ref)
		with self.assertRaises(frappe.ValidationError):
			confirm_embedded_payment(request.name, self.checkout_result(other, payment_id))

	def test_a_result_without_a_payment_is_rejected(self):
		request = self.create_embedded_payment()
		with self.assertRaises(frappe.ValidationError):
			confirm_embedded_payment(request.name, {"razorpay_signature": ["not", "text"]})

	def test_a_signed_payment_razorpay_has_not_captured_stays_pending(self):
		request = self.create_embedded_payment()
		payment_id = StubRazorpayClient.add_payment(request.order_ref, "authorized")
		self.assertEqual(
			confirm_embedded_payment(request.name, self.checkout_result(request, payment_id)), "Pending"
		)

	def test_a_paid_order_is_found_by_the_status_sync(self):
		request = self.create_embedded_payment()
		request.sync_status()
		self.assertEqual(request.status, "Pending")
		payment_id = StubRazorpayClient.pay(request.order_ref, {"method": "card", "card": {"type": "debit"}})
		request.sync_status()
		self.assertEqual(
			(request.status, request.gateway_transaction_ref, request.payment_method),
			("Paid", payment_id, "card_debit"),
		)

	def test_an_order_is_never_cancelled(self):
		request = self.create_embedded_payment()
		self.assertFalse(request.get_gateway_settings().cancel_session(request.order_ref))

	def post_order_event(self, payload: bytes):
		headers = {
			"X-Razorpay-Signature": sign_payload(payload, RAZORPAY_WEBHOOK_SECRET),
			"X-Razorpay-Event-Id": f"evt_{frappe.generate_hash(length=12)}",
		}
		testing.post_webhook(RAZORPAY_GATEWAY, payload, headers)

	def test_order_paid_webhook_marks_the_request_paid(self):
		request = self.create_embedded_payment()
		payment_id = StubRazorpayClient.pay(request.order_ref)
		self.post_order_event(build_order_paid_event(request.order_ref, payment_id))
		request.reload()
		self.assertEqual((request.status, request.gateway_transaction_ref), ("Paid", payment_id))

	def test_payment_captured_webhook_marks_the_request_paid(self):
		request = self.create_embedded_payment()
		payment_id = StubRazorpayClient.pay(request.order_ref)
		self.post_order_event(build_order_paid_event(request.order_ref, payment_id, "payment.captured"))
		self.assertEqual(frappe.db.get_value("Gateway Payment Request", request.name, "status"), "Paid")

	def test_an_order_event_for_another_checkout_is_ignored(self):
		settings = frappe.get_single("Razorpay Gateway Settings")
		payload = build_order_paid_event("order_payment_link_internal", "pay_x", "payment.captured")
		headers = {
			"X-Razorpay-Signature": sign_payload(payload, RAZORPAY_WEBHOOK_SECRET),
			"X-Razorpay-Event-Id": "evt_other_checkout",
		}
		self.assertIsNone(settings.handle_webhook(payload, headers))

	def test_a_paid_order_is_refunded_against_its_captured_payment(self):
		request = self.create_embedded_payment(550)
		payment_id = StubRazorpayClient.pay(request.order_ref)
		request.sync_status()
		refund(request.name, 200)
		self.assertEqual(StubRazorpayClient.created_refunds, [{"payment_id": payment_id, "amount": 20000}])
		self.assertEqual(frappe.db.get_value("Gateway Payment Request", request.name, "refund_amount"), 200)


class TestEmbeddedCheckoutFallback(RazorpayTestCase):
	def setUp(self):
		super().setUp()
		StubStripeClient.reset()
		configure_stripe_gateway()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		remove_stripe_gateway()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	def test_a_gateway_without_embedded_checkout_redirects(self):
		request = create_payment("Currency", "USD", 50, "USD", STRIPE_GATEWAY, checkout="embedded")
		self.assertEqual(request.checkout, "Redirect")
		self.assertTrue(request.order_url)
		self.assertFalse(request.checkout_options)
