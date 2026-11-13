# Copyright (c) 2026, Build With Hussain and contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.geo.doctype.currency.currency import Currency
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from bwh_payments.base_class import WebhookEvent
from bwh_payments.bwh_payments import webhook
from bwh_payments.bwh_payments.doctype.gateway_payment_request.test_gateway_payment_request import (
	GATEWAY,
	WEBHOOK_SECRET,
	configure_stripe_gateway,
	make_payment_request,
	remove_stripe_gateway,
)
from bwh_payments.services.stripe.stub import (
	StubStripeClient,
	build_checkout_completed_event,
	build_refund_event,
	sign_payload,
)


class TestWebhook(IntegrationTestCase):
	def setUp(self):
		StubStripeClient.reset()
		configure_stripe_gateway()
		self.original_request = getattr(frappe.local, "request", None)
		self.addCleanup(self.restore_request)

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		# Webhook logs commit, so they outlive the class rollback and would read as redeliveries.
		frappe.db.rollback()
		remove_stripe_gateway()
		frappe.db.commit()

	def restore_request(self):
		frappe.local.request = self.original_request

	def post_webhook(self, gateway: str, payload: bytes, signature: str | None):
		headers = {"Content-Type": "application/json"}
		if signature is not None:
			headers["Stripe-Signature"] = signature
		builder = EnvironBuilder(
			method="POST", path="/api/method/bwh_payments.bwh_payments.webhook.handle", data=payload
		)
		builder.headers.extend(headers)
		environ = builder.get_environ()
		environ["QUERY_STRING"] = f"gateway={gateway}"
		frappe.local.request = Request(environ)
		# Normally set by the WSGI handler; the rate limiter counts against it.
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.response = frappe._dict()
		return webhook.handle()

	def get_status_code(self):
		return frappe.local.response.get("http_status_code")

	def test_a_correctly_signed_event_marks_the_request_paid(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref)
		StubStripeClient.pay(payment_request.order_ref)

		response = self.post_webhook(GATEWAY, payload, sign_payload(payload, WEBHOOK_SECRET))

		self.assertEqual(response["status"], "ok")
		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")

	def test_a_forged_signature_is_rejected_and_nothing_is_marked_paid(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref)
		forged = sign_payload(payload, "whsec_attacker_guess")

		response = self.post_webhook(GATEWAY, payload, forged)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unsigned_delivery_is_rejected(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref)

		response = self.post_webhook(GATEWAY, payload, None)

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")

	def test_an_unknown_gateway_is_rejected(self):
		payload = build_checkout_completed_event("cs_test_nothing")

		response = self.post_webhook("Not A Gateway", payload, sign_payload(payload, WEBHOOK_SECRET))

		self.assertEqual(response["status"], "error")
		self.assertEqual(self.get_status_code(), 400)

	def test_every_verification_failure_answers_with_the_same_opaque_400(self):
		payload = build_checkout_completed_event("cs_test_nothing")

		unknown_gateway = self.post_webhook("Not A Gateway", payload, None)
		unsigned = self.post_webhook(GATEWAY, payload, None)
		forged = self.post_webhook(GATEWAY, payload, sign_payload(payload, "whsec_attacker_guess"))

		self.assertEqual(unknown_gateway, unsigned)
		self.assertEqual(unsigned, forged)
		self.assertEqual(self.get_status_code(), 400)

	def test_a_rejection_does_not_echo_the_gateway_error_back_to_the_caller(self):
		payload = build_checkout_completed_event("cs_test_nothing")

		self.post_webhook(GATEWAY, payload, None)

		self.assertEqual(frappe.local.message_log, [])

	def test_a_replayed_delivery_is_accepted_but_applied_once(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref, event_id="evt_replay")
		signature = sign_payload(payload, WEBHOOK_SECRET)
		StubStripeClient.pay(payment_request.order_ref)

		self.post_webhook(GATEWAY, payload, signature)
		second = self.post_webhook(GATEWAY, payload, signature)

		# Still a 200: anything else and Stripe retries the same event forever.
		self.assertEqual(second["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.last_webhook_event_id, "evt_replay")

	def test_the_webhook_log_never_stores_the_raw_payload(self):
		payment_request = make_payment_request(100, "SAR")
		payload = build_checkout_completed_event(payment_request.order_ref, event_id="evt_privacy")
		StubStripeClient.pay(payment_request.order_ref)

		self.post_webhook(GATEWAY, payload, sign_payload(payload, WEBHOOK_SECRET))

		logged = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{GATEWAY} Webhook"},
			fields=["data"],
		)
		self.assertTrue(logged)
		for row in logged:
			self.assertNotIn("payment_status", row.data)

	def get_webhook_logs(self, event_id: str) -> list:
		return frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": f"{GATEWAY} Webhook", "request_id": event_id},
			fields=["name", "status", "reference_docname"],
		)

	def post_paid_event(self, payment_request, event_id: str):
		payload = build_checkout_completed_event(payment_request.order_ref, event_id=event_id)
		StubStripeClient.pay(payment_request.order_ref)
		return self.post_webhook(GATEWAY, payload, sign_payload(payload, WEBHOOK_SECRET))

	def test_a_document_that_fails_to_settle_is_logged_and_the_gateway_still_gets_200(self):
		payment_request = make_payment_request(100, "SAR")

		with patch.object(Currency, "on_payment_authorized", create=True, side_effect=Exception("broken")):
			response = self.post_paid_event(payment_request, "evt_settle_fails")

		self.assertEqual(response["status"], "ok")
		self.assertIsNone(self.get_status_code())
		payment_request.reload()
		self.assertEqual(payment_request.status, "Pending")
		[log] = self.get_webhook_logs("evt_settle_fails")
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.reference_docname, payment_request.name)

	def test_a_document_that_fails_to_settle_leaves_no_commit_callbacks_behind(self):
		payment_request = make_payment_request(100, "SAR")
		ran = []

		def queue_callbacks_then_fail(*args):
			frappe.db.after_commit.add(lambda: ran.append("after_commit"))
			frappe.db.after_rollback.add(lambda: ran.append("after_rollback"))
			raise Exception("broken")

		with patch.object(
			Currency, "on_payment_authorized", create=True, side_effect=queue_callbacks_then_fail
		):
			self.post_paid_event(payment_request, "evt_settle_fails_callbacks")
		# What the job runner's commit would do once the delivery is marked Failed.
		frappe.db.after_commit.run()

		self.assertEqual(ran, ["after_rollback"])

	def test_a_settled_document_keeps_its_commit_callbacks(self):
		payment_request = make_payment_request(100, "SAR")
		ran = []

		def queue_callback(*args):
			frappe.db.after_commit.add(lambda: ran.append("after_commit"))

		with patch.object(Currency, "on_payment_authorized", create=True, side_effect=queue_callback):
			self.post_paid_event(payment_request, "evt_settles_callbacks")
		frappe.db.after_commit.run()

		self.assertEqual(ran, ["after_commit"])

	def test_a_failed_delivery_settles_when_retried_after_the_fix(self):
		payment_request = make_payment_request(100, "SAR")
		with patch.object(Currency, "on_payment_authorized", create=True, side_effect=Exception("broken")):
			self.post_paid_event(payment_request, "evt_retry")
		[log] = self.get_webhook_logs("evt_retry")

		webhook.retry_webhook(log.name)

		payment_request.reload()
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(frappe.db.get_value("Integration Request", log.name, "status"), "Completed")

	def test_only_a_system_manager_can_retry_a_delivery(self):
		payment_request = make_payment_request(100, "SAR")
		with patch.object(Currency, "on_payment_authorized", create=True, side_effect=Exception("broken")):
			self.post_paid_event(payment_request, "evt_retry_guest")
		[log] = self.get_webhook_logs("evt_retry_guest")

		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			webhook.retry_webhook(log.name)

	def test_a_redelivered_event_is_queued_once(self):
		payment_request = make_payment_request(100, "SAR")

		self.post_paid_event(payment_request, "evt_twice")
		self.post_paid_event(payment_request, "evt_twice")

		self.assertEqual(len(self.get_webhook_logs("evt_twice")), 1)

	def test_a_deadlock_is_handed_back_to_the_job_runner_to_retry(self):
		payment_request = make_payment_request(100, "SAR")
		with (
			patch.object(
				type(payment_request), "apply_webhook_status", side_effect=frappe.QueryDeadlockError
			),
			self.assertRaises(frappe.RetryBackgroundJobError),
		):
			webhook.process_webhook(
				webhook.log_webhook(
					GATEWAY,
					event=WebhookEvent(
						session_id=payment_request.order_ref, status="Paid", event_id="evt_deadlock"
					),
					reference_docname=payment_request.name,
				).name
			)

	def test_a_succeeded_refund_event_settles_the_pending_refund(self):
		payment_request = make_payment_request(100, "SAR")
		StubStripeClient.pay(payment_request.order_ref)
		payment_request.sync_status()
		StubStripeClient.next_refund_status = "pending"
		refund = payment_request.create_refund(30)
		payload = build_refund_event(
			refund.refund_id, payment_request.gateway_transaction_ref, "succeeded", 3000, "sar"
		)

		response = self.post_webhook(GATEWAY, payload, sign_payload(payload, WEBHOOK_SECRET))

		self.assertEqual(response["status"], "ok")
		refund.reload()
		payment_request.reload()
		self.assertEqual(refund.status, "Processed")
		self.assertEqual(flt(payment_request.refund_amount), 30)
