from abc import ABC, abstractmethod

from bwh_payments.services import GatewayModel


class RazorpayPayment(GatewayModel):
	payment_id: str
	status: str | None = None


class RazorpayPaymentLink(GatewayModel):
	id: str
	status: str | None = None
	short_url: str | None = None
	payments: list[RazorpayPayment] | None = None


class RazorpayRefund(GatewayModel):
	id: str | None = None
	status: str | None = None
	# Minor units, as Razorpay echoes it.
	amount: int | None = None


class RazorpayPaymentDetails(GatewayModel):
	id: str
	# Minor units.
	amount: int
	amount_refunded: int = 0


class RazorpayTransfer(GatewayModel):
	"""A Route transfer: part of a payment sent on to a linked account (`recipient`). Minor units."""

	id: str
	recipient: str
	amount: int
	amount_reversed: int = 0
	status: str | None = None
	settlement_status: str | None = None


class RazorpayEntity(GatewayModel):
	"""An account or stakeholder: we keep only its id."""

	id: str


class RazorpayProduct(GatewayModel):
	"""A linked account's Route product configuration, which carries its activation status."""

	id: str
	activation_status: str | None = None
	requirements: list[dict] | None = None


class BaseRazorpayClient(ABC):
	"""Razorpay's Payment Links, Refunds and Route APIs. A rejected call raises `frappe.ValidationError`."""

	@abstractmethod
	def create_payment_link(self, payload: dict) -> RazorpayPaymentLink: ...

	@abstractmethod
	def get_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def cancel_payment_link(self, link_id: str) -> RazorpayPaymentLink: ...

	@abstractmethod
	def create_refund(self, payment_id: str, amount: int) -> RazorpayRefund:
		"""Refund `amount` minor units of a captured payment."""

	@abstractmethod
	def get_refund(self, refund_id: str) -> RazorpayRefund: ...

	@abstractmethod
	def get_payment(self, payment_id: str) -> RazorpayPaymentDetails: ...

	@abstractmethod
	def get_payment_transfers(self, payment_id: str) -> list[RazorpayTransfer]: ...

	@abstractmethod
	def reverse_transfer(self, transfer_id: str, amount: int) -> None:
		"""Pull `amount` minor units back from the linked account into our balance."""

	@abstractmethod
	def update_transfer(self, transfer_id: str, payload: dict) -> RazorpayTransfer: ...

	@abstractmethod
	def create_account(self, payload: dict) -> RazorpayEntity: ...

	@abstractmethod
	def update_account(self, account_id: str, payload: dict) -> RazorpayEntity: ...

	@abstractmethod
	def create_stakeholder(self, account_id: str, payload: dict) -> RazorpayEntity: ...

	@abstractmethod
	def update_stakeholder(self, account_id: str, stakeholder_id: str, payload: dict) -> RazorpayEntity: ...

	@abstractmethod
	def request_product(self, account_id: str, payload: dict) -> RazorpayProduct: ...

	@abstractmethod
	def update_product(self, account_id: str, product_id: str, payload: dict) -> RazorpayProduct: ...

	@abstractmethod
	def get_product(self, account_id: str, product_id: str) -> RazorpayProduct: ...
