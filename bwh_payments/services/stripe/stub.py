import hashlib
import hmac
import json
import time
from typing import ClassVar

import frappe
import stripe

from bwh_payments.services.stripe.base import BaseStripeClient, StripeCheckoutSession, StripeRefund


class StubStripeClient(BaseStripeClient):
	"""In-memory Stripe, selected under tests. Drive outcomes through the classmethod controls; reset()
	between tests. Recordings keep Stripe's own request shapes."""

	sessions: ClassVar[dict[str, StripeCheckoutSession]] = {}
	created_sessions: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	next_refund_status: ClassVar[str] = "succeeded"

	@classmethod
	def reset(cls):
		cls.sessions = {}
		cls.created_sessions = []
		cls.created_refunds = []
		cls.next_refund_status = "succeeded"

	# --- controls -----------------------------------------------------------------------------------

	@classmethod
	def pay(cls, session_id: str):
		"""The shopper completes Checkout: the session closes with a captured PaymentIntent behind it."""
		session = cls.sessions[session_id]
		session.payment_status = "paid"
		session.status = "complete"
		session.payment_intent = f"pi_{session_id}"

	# --- API ----------------------------------------------------------------------------------------

	def create_checkout_session(self, params: dict) -> StripeCheckoutSession:
		self.created_sessions.append(params)
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		session_id = f"cs_test_{frappe.generate_hash(length=12)}"
		self.sessions[session_id] = StripeCheckoutSession(
			id=session_id,
			url=f"https://checkout.stripe.test/{session_id}",
			status="open",
			payment_status="unpaid",
			currency=params["line_items"][0]["price_data"]["currency"],
			success_url=params["success_url"],
			cancel_url=params["cancel_url"],
		)
		return self.retrieve_checkout_session(session_id)

	def retrieve_checkout_session(self, session_id: str) -> StripeCheckoutSession:
		if session_id not in self.sessions:
			raise stripe.InvalidRequestError(f"No such checkout.session: '{session_id}'", param="session")
		return self.sessions[session_id].model_copy()

	def expire_checkout_session(self, session_id: str) -> StripeCheckoutSession:
		session = self.retrieve_checkout_session(session_id)
		if session.status != "open":
			raise stripe.InvalidRequestError(
				f"session is {session.status} and cannot be expired", param="session"
			)
		self.sessions[session_id].status = "expired"
		return self.retrieve_checkout_session(session_id)

	def create_refund(self, params: dict) -> StripeRefund:
		self.created_refunds.append(params)
		return StripeRefund(
			id=f"re_test_{frappe.generate_hash(length=12)}",
			status=self.next_refund_status,
			amount=params["amount"],
		)


def sign_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
	"""A genuine Stripe-Signature header, so tests exercise signature verification instead of bypassing it."""
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
