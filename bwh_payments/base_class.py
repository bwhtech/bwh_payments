from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, model_validator


class GatewayResult(BaseModel):
	# Rejecting a numeric refund id after money moved would leave the refund unrecorded.
	model_config = ConfigDict(coerce_numbers_to_str=True)


class CheckoutSession(GatewayResult):
	session_id: str
	redirect_url: str
	success_url: str | None = None
	cancel_url: str | None = None
	failure_url: str | None = None


class InlineCheckoutDisplay(BaseModel):
	"""What the caller shows in the in-page checkout. Each gateway maps what its SDK supports."""

	# Autoincrement document names arrive as ints.
	model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

	name: str | None = None
	description: str | None = None
	# A site-relative path or an absolute URL.
	image: str | None = None
	# Seconds until the checkout closes itself, e.g. when the caller's hold on the goods ends.
	timeout: int | None = None
	notes: dict[str, str] = {}
	# The prefilled email and name cannot be changed in the checkout.
	lock_customer: bool = False


class InlineCheckout(BaseModel):
	"""What the caller's page opens the gateway's browser SDK with. Never carries a secret."""

	request: str
	sdk: str
	script_url: str
	options: dict
	# Where the shopper goes once paid, even when the confirm call fails: the webhook settles it.
	redirect_to: str | None = None


class InlineCheckoutGateway(ABC):
	"""Mixed into a gateway whose checkout opens in the caller's page through its browser SDK.

	The caller names the SDKs it can open (`create_payment(inline_sdks=...)`); any other gateway,
	or one the caller cannot open, gets a link checkout as usual.
	"""

	sdk: str
	script_url: str

	@abstractmethod
	def create_inline_session(
		self, amount: float, currency: str, reference: str | None = None, customer: dict | None = None
	) -> str:
		"""Open the gateway's checkout and return its id, kept as the request's `order_ref`."""

	@abstractmethod
	def get_inline_options(self, payment_request, display: InlineCheckoutDisplay) -> BaseModel:
		"""What the SDK opens the checkout with, as the gateway's own typed model."""

	@abstractmethod
	def verify_inline_payment(self, session_id: str, response: dict) -> bool:
		"""Whether the SDK's response was signed by the gateway for this session."""


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
