import hashlib
from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel


def payu_hash(*parts: str) -> str:
	"""PayU signs every message with sha512 over its fields joined by `|`."""
	return hashlib.sha512("|".join(parts).encode()).hexdigest()


class PayUTransaction(GatewayModel):
	"""One `transaction_details` entry of `verify_payment`."""

	mihpayid: str | None = None
	status: str | None = None
	unmappedstatus: str | None = None


class PayURefund(GatewayModel):
	# Read after money moved, so every field is optional.
	request_id: str | None = None
	status: str | None = None
	# Major units.
	amt: str | None = None
	mihpayid: str | None = None


class BasePayUClient(ABC):
	"""PayU's merchant postservice commands. A refused command raises `frappe.ValidationError`."""

	@abstractmethod
	def verify_payment(self, txnid: str) -> PayUTransaction:
		"""An unknown txnid comes back with status `Not Found`, not as an error."""

	@abstractmethod
	def refund(self, mihpayid: str, token: str, amount: str) -> PayURefund:
		"""Refund `amount` (a major-unit decimal string) of a captured payment."""

	@abstractmethod
	def get_refund(self, request_id: str) -> PayURefund: ...
