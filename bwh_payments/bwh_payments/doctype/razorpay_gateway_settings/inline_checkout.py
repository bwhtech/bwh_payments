import hashlib
import hmac

from frappe import _
from frappe.utils import get_url, validate_email_address
from frappe.utils.data import flt
from pydantic import BaseModel

from bwh_payments.base_class import InlineCheckoutDisplay, InlineCheckoutGateway
from bwh_payments.currency import to_minor_units, validate_transaction_currency
from bwh_payments.services.razorpay.base import RazorpayPayment

# Inline checkouts are Razorpay Orders; Payment Links start with `plink_`.
RAZORPAY_ORDER_PREFIX = "order_"
RAZORPAY_ORDER_PAID_STATUS = "paid"


class RazorpayCheckoutOptions(BaseModel):
	"""Razorpay Standard Checkout options. An unset one falls back to the Razorpay dashboard."""

	key: str
	order_id: str
	amount: int
	currency: str
	description: str
	name: str | None = None
	image: str | None = None
	prefill: dict[str, str] = {}
	notes: dict[str, str] = {}
	timeout: int | None = None
	readonly: dict[str, bool] | None = None
	modal: dict[str, bool] = {"confirm_close": True}


class RazorpayInlineCheckout(InlineCheckoutGateway):
	"""Razorpay Standard Checkout: the shopper pays for an Order in a modal on the caller's page."""

	sdk = "razorpay"
	script_url = "https://checkout.razorpay.com/v1/checkout.js"

	def create_inline_session(
		self, amount: float, currency: str, reference: str | None = None, customer: dict | None = None
	) -> str:
		currency = currency or self.currency
		validate_transaction_currency(currency)
		order = self.get_client().create_order(
			{
				"amount": to_minor_units(amount, currency),
				"currency": currency.upper(),
				"receipt": reference,
				"notes": {"reference_id": reference or ""},
			}
		)
		return order.id

	def get_inline_options(self, payment_request, display: InlineCheckoutDisplay) -> RazorpayCheckoutOptions:
		notes = {"reference_id": payment_request.name, **display.notes}
		return RazorpayCheckoutOptions(
			key=self.key_id,
			order_id=payment_request.order_ref,
			amount=to_minor_units(flt(payment_request.amount), payment_request.currency_code),
			currency=payment_request.currency_code.upper(),
			name=display.name,
			description=display.description or _("Online order payment"),
			image=get_url(display.image) if display.image else None,
			prefill=get_customer_details(payment_request.get_customer_details()),
			notes=notes,
			timeout=display.timeout,
			readonly={"email": True, "name": True} if display.lock_customer else None,
		)

	def verify_inline_payment(self, session_id: str, response: dict) -> bool:
		if response.get("razorpay_order_id") != session_id:
			return False
		message = f"{session_id}|{response.get('razorpay_payment_id') or ''}"
		expected = hmac.new(self.get_password("key_secret").encode(), message.encode(), hashlib.sha256)
		signature = str(response.get("razorpay_signature") or "")
		# compare_digest raises TypeError on a non-ASCII str.
		return signature.isascii() and hmac.compare_digest(expected.hexdigest(), signature)

	def get_order_status(self, order_id: str) -> str:
		order = self.get_client().get_order(order_id)
		if (order.status or "").strip().casefold() == RAZORPAY_ORDER_PAID_STATUS:
			return "Paid"
		# An order turns paid only on capture, hence the same opt-in switch as for links.
		if self.treat_authorised_as_paid and has_authorised_payment(self.get_session_payments(order_id)):
			return "Paid"
		return "Pending"

	def get_session_payments(self, session_id: str) -> list[RazorpayPayment]:
		client = self.get_client()
		if is_order(session_id):
			return client.get_order_payments(session_id)
		return client.get_payment_link(session_id).payments or []


def is_order(session_id: str) -> bool:
	return session_id.startswith(RAZORPAY_ORDER_PREFIX)


def has_authorised_payment(payments: list[RazorpayPayment]) -> bool:
	return any(has_status(payment, "authorized") for payment in payments)


def has_status(payment: RazorpayPayment, status: str) -> bool:
	return (payment.status or "").strip().casefold() == status


def get_customer_details(customer: dict) -> dict:
	"""Razorpay rejects a blank `contact` or `email`, so empty values are dropped."""
	details = {
		"name": get_customer_name(customer),
		"email": validate_email_address(customer.get("email") or "", throw=False),
		# Frappe stores `+91-9876543210`; Razorpay ignores a contact with the hyphen.
		"contact": (customer.get("phone") or "").replace("-", ""),
	}
	return {key: value for key, value in details.items() if value}


def get_customer_name(customer: dict) -> str:
	name = customer.get("name") or {}
	return " ".join(part for part in (name.get("forenames"), name.get("surname")) if part)
