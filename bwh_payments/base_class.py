from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GatewayResult(BaseModel):
	# Rejecting a numeric refund id after money moved would leave the refund unrecorded.
	model_config = ConfigDict(coerce_numbers_to_str=True)


class CheckoutSession(GatewayResult):
	session_id: str
	redirect_url: str
	success_url: str | None = None
	cancel_url: str | None = None
	failure_url: str | None = None


class RefundResult(GatewayResult):
	# All optional: read after money moved, so a thin result must not fail.
	refund_id: str | None = None
	status: str | None = None
	amount: float | None = None


class WebhookEvent(GatewayResult):
	"""A payment event carries `session_id`; a refund event carries `refund_id` instead.

	With `sync_status`, the request asks the gateway instead of applying `status`. A gateway whose
	payment still needs a capture uses it, so money moves in our own status sync, never in a guest request.
	"""

	session_id: str | None = None
	status: str
	sync_status: bool = False
	event_id: str | None = None
	refund_id: str | None = None
	# Major units. Needed to record a refund raised outside this app.
	refund_amount: float | None = None
	# The settled payment's id. On a refund event it finds the request for a refund raised outside this
	# app; on a payment event it is kept for gateways that cannot be asked for it later.
	transaction_ref: str | None = None

	@model_validator(mode="after")
	def require_session_or_refund(self):
		if not (self.session_id or self.refund_id):
			raise ValueError("A webhook event needs a session_id or a refund_id")
		return self


class PaymentSplit(GatewayResult):
	"""Part of a payment the gateway sends on to a linked account. `amount` is in major units."""

	account_id: str
	amount: float
	# Timezone-aware. None: the share is paid out on the gateway's usual schedule.
	hold_until: datetime | None = None


class SplitTransfer(GatewayResult):
	"""What the gateway made of a split once the payment was captured."""

	account_id: str
	transfer_id: str
	transfer_status: str | None = None
	settlement_status: str | None = None


class LinkedAccountState(GatewayResult):
	"""A linked account as the gateway reports it. `status` is a Gateway Linked Account status."""

	status: str
	gateway_status: str | None = None
	requirements: list[dict] = Field(default_factory=list)


class GatewayOption(BaseModel):
	"""A gateway as a storefront lists it. Logos are site-relative asset paths."""

	name: str
	title: str
	logo: str | None = None
	logo_dark: str | None = None


class GatewayInfo(BaseModel):
	"""A gateway this app can take payments through, as a settings screen lists it, set up or not."""

	name: str
	settings_doctype: str
	description: str | None = None
	docs_url: str | None = None
	reusable_checkout: bool = False
	logo: str | None = None
	logo_dark: str | None = None


class PaymentGatewayBase(ABC):
	"""Contract for every `<Gateway> Gateway Settings` Single. Amounts are in major units (12.34)."""

	logo: str | None = None
	logo_dark: str | None = None
	# One line for a merchant choosing a gateway, and where they find its credentials.
	description: str | None = None
	docs_url: str | None = None
	# The checkout link stays payable after the shopper leaves it, so a caller can send them back to it.
	reusable_checkout: bool = False

	@abstractmethod
	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
	) -> CheckoutSession:
		"""With `return_url`, every way back from the checkout (paid, failed, cancelled) goes there."""

	@abstractmethod
	def get_payment_status(self, session_id: str) -> str:
		"""Return a Gateway Payment Request `status` value."""

	@abstractmethod
	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult: ...

	@abstractmethod
	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		"""Verify the signature, then return the event, or None to ignore the delivery."""

	def cancel_session(self, session_id: str) -> bool:
		return False

	def supported_currencies(self) -> tuple[str, ...] | None:
		"""ISO codes this gateway can charge in, or None for any currency the site has."""
		return None

	def split_currencies(self) -> tuple[str, ...]:
		"""ISO codes in which `create_session` can send part of a payment to linked accounts."""
		return ()

	def get_split_transfers(self, session_id: str) -> list[SplitTransfer] | None:
		"""The transfers the gateway made for a paid session's splits, or None when it cannot say."""
		return None

	def set_split_hold(self, transfer_id: str, hold_until: datetime | None) -> None:
		raise NotImplementedError

	def submit_linked_account(self, account, details: dict) -> None:
		"""Create `account` (a Gateway Linked Account) on the gateway, or update what the gateway holds.

		Store each id the gateway hands back as soon as it exists, so a failure halfway can resume.
		"""
		raise NotImplementedError

	def get_linked_account_state(self, account) -> LinkedAccountState:
		raise NotImplementedError

	def get_refund_status(self, session_id: str, refund_id: str) -> RefundResult | None:
		"""What the gateway now says about a refund, or None when it cannot be asked."""
		return None

	def list_refunds(self, session_id: str) -> list[RefundResult] | None:
		"""Every refund the gateway holds for the payment, or None when it cannot list them."""
		return None

	def get_transaction_ref(self, session_id: str) -> str | None:
		"""The gateway's id for the settled payment, or None when it cannot say."""
		return None

	def get_gateway_name(self) -> str:
		return self.__class__.__name__
