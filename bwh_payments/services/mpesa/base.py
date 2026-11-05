from abc import ABC, abstractmethod

import frappe
from pydantic import ConfigDict, Field

from bwh_payments.services import GatewayModel


class MpesaStillProcessingError(frappe.ValidationError):
	"""Daraja's answer to an STK query asked before the prompt has a result."""


class MpesaModel(GatewayModel):
	"""Daraja names fields in PascalCase; they are read through aliases."""

	model_config = ConfigDict(validate_by_name=True)


class MpesaStkPush(MpesaModel):
	checkout_request_id: str = Field(alias="CheckoutRequestID")


class MpesaStkQuery(MpesaModel):
	result_code: str = Field(alias="ResultCode")


class MpesaReversal(MpesaModel):
	conversation_id: str = Field(alias="ConversationID")


class BaseMpesaClient(ABC):
	"""Safaricom's Daraja STK Push and Reversal APIs. A rejected call raises `frappe.ValidationError`.

	The client adds the shortcode, STK password and timestamp, and the reversal initiator credentials;
	payloads carry the rest.
	"""

	@abstractmethod
	def send_stk_push(self, payload: dict) -> MpesaStkPush: ...

	@abstractmethod
	def query_stk_push(self, checkout_request_id: str) -> MpesaStkQuery:
		"""Raises `MpesaStillProcessingError` while the shopper has not answered the prompt."""

	@abstractmethod
	def reverse_transaction(self, payload: dict) -> MpesaReversal:
		"""Accepted only; Safaricom posts the result to the payload's `ResultURL` later."""
