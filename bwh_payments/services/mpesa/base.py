from abc import ABC, abstractmethod

from pydantic import ConfigDict, Field

from bwh_payments.services import GatewayModel


class MpesaModel(GatewayModel):
	"""Daraja names fields in PascalCase; they are read through aliases."""

	model_config = ConfigDict(validate_by_name=True)


class MpesaStkPush(MpesaModel):
	checkout_request_id: str = Field(alias="CheckoutRequestID")
	merchant_request_id: str | None = Field(default=None, alias="MerchantRequestID")


class MpesaStkQuery(MpesaModel):
	# None while the shopper has not answered the prompt yet.
	result_code: str | None = Field(default=None, alias="ResultCode")
	result_desc: str | None = Field(default=None, alias="ResultDesc")


class MpesaReversal(MpesaModel):
	conversation_id: str = Field(alias="ConversationID")
	originator_conversation_id: str | None = Field(default=None, alias="OriginatorConversationID")


class BaseMpesaClient(ABC):
	"""Safaricom's Daraja STK Push and Reversal APIs. A rejected call raises `frappe.ValidationError`.

	The client adds the shortcode, STK password and timestamp, and the reversal initiator credentials;
	payloads carry the rest.
	"""

	@abstractmethod
	def send_stk_push(self, payload: dict) -> MpesaStkPush: ...

	@abstractmethod
	def query_stk_push(self, checkout_request_id: str) -> MpesaStkQuery: ...

	@abstractmethod
	def reverse_transaction(self, payload: dict) -> MpesaReversal:
		"""Accepted only; Safaricom posts the result to the payload's `ResultURL` later."""
