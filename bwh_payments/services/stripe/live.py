import stripe

from bwh_payments.services.stripe.base import BaseStripeClient, StripeCheckoutSession, StripeRefund


class LiveStripeClient(BaseStripeClient):
	def __init__(self, api_key: str):
		# Per instance, never the module-level `stripe.api_key`, so two accounts cannot clobber each other.
		self.client = stripe.StripeClient(api_key)

	def create_checkout_session(self, params: dict) -> StripeCheckoutSession:
		return parse_session(self.client.v1.checkout.sessions.create(params))

	def retrieve_checkout_session(self, session_id: str) -> StripeCheckoutSession:
		return parse_session(self.client.v1.checkout.sessions.retrieve(session_id))

	def expire_checkout_session(self, session_id: str) -> StripeCheckoutSession:
		return parse_session(self.client.v1.checkout.sessions.expire(session_id))

	def create_refund(self, params: dict) -> StripeRefund:
		return StripeRefund.model_validate(self.client.v1.refunds.create(params), from_attributes=True)


def parse_session(session) -> StripeCheckoutSession:
	# StripeObject is attribute-based, not a dict, since SDK 15.
	return StripeCheckoutSession.model_validate(session, from_attributes=True)
