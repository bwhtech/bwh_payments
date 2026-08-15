"""A stand-in for the Stripe SDK transport.

The gateway controller runs its real code — amount conversion, URL building, response mapping — against
this instead of the network, so tests exercise the shipped logic and never touch a live account.
"""

import hashlib
import hmac
import json
import time
from typing import ClassVar

import frappe
from frappe import _dict


class FakeStripeSession(_dict):
	pass


class FakeStripeClient:
	"""Records what the controller sent so a test can assert on the exact minor-unit amount."""

	created_sessions: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	sessions: ClassVar[dict[str, dict]] = {}
	next_refund_status = "succeeded"
	refund_counter = 0

	def __init__(self, api_key=None):
		self.api_key = api_key
		self.checkout = _dict(sessions=FakeSessionService())
		self.refunds = FakeRefundService()

	@classmethod
	def reset(cls):
		cls.created_sessions = []
		cls.created_refunds = []
		cls.sessions = {}
		cls.next_refund_status = "succeeded"
		cls.refund_counter = 0

	@classmethod
	def register_paid_session(cls, session_id: str, currency: str = "sar"):
		cls.sessions[session_id] = {
			"id": session_id,
			"payment_status": "paid",
			"status": "complete",
			"payment_intent": f"pi_{session_id}",
			"currency": currency,
		}


class FakeSessionService:
	def create(self, params):
		FakeStripeClient.created_sessions.append(params)
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		session_id = f"cs_test_{frappe.generate_hash(length=12)}"
		FakeStripeClient.sessions[session_id] = {
			"id": session_id,
			"payment_status": "unpaid",
			"status": "open",
			"payment_intent": None,
			"currency": params["line_items"][0]["price_data"]["currency"],
		}
		return FakeStripeSession(
			id=session_id,
			url=f"https://checkout.stripe.test/{session_id}",
			success_url=params["success_url"],
			cancel_url=params["cancel_url"],
		)

	def retrieve(self, session_id):
		session = FakeStripeClient.sessions.get(session_id)
		if not session:
			raise KeyError(f"unknown session {session_id}")
		return FakeStripeSession(**session)


class FakeRefundService:
	def create(self, params):
		FakeStripeClient.created_refunds.append(params)
		FakeStripeClient.refund_counter += 1
		return FakeStripeSession(
			id=f"re_test_{frappe.generate_hash(length=12)}",
			status=FakeStripeClient.next_refund_status,
			amount=params["amount"],
		)


def sign_stripe_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
	"""Build a genuine Stripe-Signature header so signature verification is exercised, not bypassed."""
	timestamp = timestamp or int(time.time())
	signed_payload = b"%d." % timestamp + payload
	signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
	return f"t={timestamp},v1={signature}"


def build_checkout_completed_event(session_id: str, event_id: str = "evt_test_0001") -> bytes:
	return json.dumps(
		{
			"id": event_id,
			"type": "checkout.session.completed",
			"data": {"object": {"id": session_id, "payment_status": "paid"}},
		}
	).encode()
