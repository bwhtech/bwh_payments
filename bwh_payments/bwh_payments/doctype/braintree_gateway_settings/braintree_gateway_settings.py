# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from urllib.parse import parse_qs, urlencode

import braintree
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.rate_limiter import rate_limit
from frappe.sessions import get_csrf_token
from frappe.utils import get_url
from frappe.utils.data import flt
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from bwh_payments.base_class import (
	CheckoutSession,
	PaymentGatewayBase,
	RefundResult,
	WebhookEvent,
)
from bwh_payments.bwh_payments.utils import add_reference_id, get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency
from bwh_payments.services.braintree.base import BaseBraintreeClient, BraintreeResult, BraintreeTransaction
from bwh_payments.services.braintree.live import LiveBraintreeClient
from bwh_payments.services.braintree.stub import StubBraintreeClient

BRAINTREE_ENVIRONMENTS = {
	"Sandbox": braintree.Environment.Sandbox,
	"Production": braintree.Environment.Production,
}

SETTINGS_DOCTYPE = "Braintree Gateway Settings"
ENDPOINT = (
	"/api/method/bwh_payments.bwh_payments.doctype.braintree_gateway_settings.braintree_gateway_settings"
)
# Braintree has no hosted payment page, so the shopper is sent to ours, which renders Drop-in.
CHECKOUT_TEMPLATE = "bwh_payments/templates/braintree_checkout.html"
SESSION_TOKEN_LENGTH = 32
# Seconds a second `pay()` on the same session waits for the first to finish.
SESSION_LOCK_TIMEOUT = 20

# Sales are submitted for settlement, so any of these means the money is on its way.
BRAINTREE_PAID_STATUSES = (
	"submitted_for_settlement",
	"settling",
	"settled",
	"settlement_pending",
	"settlement_confirmed",
)

# Unmapped states stay Pending, declines included, so the shopper can retry another card.
BRAINTREE_STATUS_MAP = {
	**dict.fromkeys(BRAINTREE_PAID_STATUSES, "Paid"),
	"settlement_declined": "Not Paid",
	"settlement_failed": "Not Paid",
	"voided": "Cancelled",
	"authorization_expired": "Cancelled",
}

# Money has moved, so a refund is a credit against the sale.
BRAINTREE_REFUNDABLE_STATUSES = ("settling", "settled")
# Money has not moved yet, so the only refund Braintree allows is voiding the whole sale.
BRAINTREE_VOIDABLE_STATUSES = ("authorized", "submitted_for_settlement", "settlement_pending")
BRAINTREE_UNCANCELLABLE_STATUSES = (*BRAINTREE_PAID_STATUSES, "authorizing", "authorized")

# A refund credit's status in the words Gateway Refund understands; anything else stays Pending.
BRAINTREE_REFUND_STATUS_MAP = {
	"submitted_for_settlement": "processed",
	"settling": "processed",
	"settled": "processed",
	"settlement_pending": "pending",
	"settlement_declined": "failed",
	"settlement_failed": "failed",
	"voided": "cancelled",
}


class BraintreeGatewaySettings(Document, PaymentGatewayBase):
	description = "Cards with 3-D Secure, worldwide. Needs a merchant account per currency."
	docs_url = "https://developer.paypal.com/braintree/articles/control-panel/important-gateway-credentials"
	# The checkout page stays payable until the session is charged.
	reusable_checkout = True

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from bwh_payments.bwh_payments.doctype.braintree_merchant_account.braintree_merchant_account import (
			BraintreeMerchantAccount,
		)

		cancelled_url: DF.Data | None
		enabled: DF.Check
		failure_url: DF.Data | None
		merchant_accounts: DF.Table[BraintreeMerchantAccount]
		merchant_id: DF.Data
		mode: DF.Literal["Sandbox", "Production"]
		private_key: DF.Password
		public_key: DF.Data
		success_url: DF.Data | None
		three_d_secure: DF.Check
	# end: auto-generated types

	def validate(self):
		self.validate_unique_currencies()
		if self.enabled:
			self.validate_credentials()

	def validate_unique_currencies(self):
		seen = set()
		for row in self.merchant_accounts:
			if row.currency in seen:
				frappe.throw(
					_("Row {0}: currency {1} already has a merchant account.").format(row.idx, row.currency)
				)
			seen.add(row.currency)

	def validate_credentials(self):
		"""A client token per merchant account fails on bad keys or an unknown account."""
		if not self.merchant_accounts:
			frappe.throw(_("Add at least one merchant account before enabling Braintree."))

		client = self.get_client()
		for row in self.merchant_accounts:
			client.generate_client_token(row.merchant_account_id)

	def get_gateway_name(self) -> str:
		return "Braintree"

	def get_braintree_gateway(self) -> braintree.BraintreeGateway:
		# Per instance, never the module-level `Configuration.configure`, so two configs never share state.
		return braintree.BraintreeGateway(
			braintree.Configuration(
				environment=BRAINTREE_ENVIRONMENTS[self.mode or "Sandbox"],
				merchant_id=self.merchant_id,
				public_key=self.public_key,
				private_key=self.get_password("private_key"),
			)
		)

	def get_client(self) -> BaseBraintreeClient:
		if frappe.in_test:
			return StubBraintreeClient()

		return LiveBraintreeClient(self.get_braintree_gateway())

	def supported_currencies(self) -> tuple[str, ...]:
		# Each currency settles through its own merchant account.
		return tuple(row.currency.strip().upper() for row in self.merchant_accounts)

	def get_merchant_account(self, currency: str) -> str:
		requested = (currency or "").strip().upper()
		for row in self.merchant_accounts:
			if row.currency.strip().upper() == requested:
				return row.merchant_account_id

		frappe.throw(_("Braintree has no merchant account for {0}").format(frappe.bold(currency)))

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
	) -> CheckoutSession:
		validate_transaction_currency(currency)
		self.get_merchant_account(currency)

		# No Braintree object exists until the shopper pays, so the session id is ours. It is also the
		# capability for the checkout page and `pay()`, hence long and random, and the sale's `order_id`.
		session_id = frappe.generate_hash(length=SESSION_TOKEN_LENGTH)
		return CheckoutSession(
			session_id=session_id,
			redirect_url=get_url(f"{ENDPOINT}.start_checkout?{urlencode({'session': session_id})}"),
			success_url=return_url or self.build_return_url(self.success_url, _("Success URL"), reference),
			cancel_url=return_url or self.build_return_url(self.cancelled_url, _("Cancelled URL"), reference),
			failure_url=return_url or get_localised_url(self.failure_url),
		)

	def build_return_url(self, url: str, label: str, reference: str | None) -> str:
		"""The storefront's confirmation page reads `reference_id`."""
		if not url:
			frappe.throw(_("Please set the {0} in Braintree Gateway Settings").format(label))
		return add_reference_id(url, reference)

	def charge(self, session_id: str, amount: float, currency: str, nonce: str) -> BraintreeResult:
		"""One sale for the request's own amount; the caller holds the session lock."""
		options = {"submit_for_settlement": True}
		if self.three_d_secure:
			# `pay()` is a guest endpoint, so the page's own 3-D Secure check can be skipped by posting a
			# nonce straight to it. Required here, Braintree itself refuses a nonce without the liability shift.
			options["three_d_secure"] = {"required": True}
		return self.get_client().sale(
			{
				"amount": format_amount(amount, currency),
				"payment_method_nonce": nonce,
				"merchant_account_id": self.get_merchant_account(currency),
				"order_id": session_id,
				"options": options,
			}
		)

	def get_payment_status(self, session_id: str) -> str:
		transactions = self.get_client().search_sales(session_id)
		if any(transaction.status in BRAINTREE_PAID_STATUSES for transaction in transactions):
			return "Paid"
		if not transactions:
			return "Pending"

		# Earlier declines are history; the latest attempt is what the shopper is on.
		latest = max(transactions, key=lambda transaction: transaction.created_at)
		return BRAINTREE_STATUS_MAP.get(latest.status, "Pending")

	def cancel_session(self, session_id: str) -> bool:
		# Our page is the only way to pay, so holding the session lock until the caller commits its
		# Cancelled status means `pay()` cannot charge in between.
		acquire_session_lock(session_id)
		try:
			transactions = self.get_client().search_sales(session_id)
		except Exception:
			release_session_lock(session_id)
			raise

		if any(transaction.status in BRAINTREE_UNCANCELLABLE_STATUSES for transaction in transactions):
			release_session_lock(session_id)
			return False

		# Registered after the searches above: their request logs commit, and that would release it early.
		frappe.db.after_commit.add(lambda: release_session_lock(session_id))
		frappe.db.after_rollback.add(lambda: release_session_lock(session_id))
		return True

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		sale = self.get_paid_sale(session_id)
		currency = currency or sale.currency_iso_code
		client = self.get_client()

		if sale.status in BRAINTREE_REFUNDABLE_STATUSES:
			result = client.refund(sale.id, format_amount(amount, currency))
		elif sale.status in BRAINTREE_VOIDABLE_STATUSES:
			if flt(amount) < flt(sale.amount):
				frappe.throw(
					_(
						"This Braintree payment has not settled yet, so only a full refund is possible."
						" Retry the partial refund after settlement, usually the next day."
					)
				)
			result = client.void(sale.id)
		else:
			frappe.throw(_("Cannot refund a Braintree payment in status {0}").format(sale.status))

		if not result.is_success:
			frappe.throw(_("Braintree refund failed: {0}").format(result.message))

		refund = result.transaction
		return RefundResult(
			refund_id=refund.id,
			status=read_refund_status(refund),
			amount=flt(refund.amount) if refund.amount else flt(amount),
		)

	def get_refund_status(self, session_id: str, refund_id: str) -> RefundResult:
		refund = self.get_client().find_transaction(refund_id)
		return RefundResult(refund_id=refund.id, status=read_refund_status(refund))

	def get_transaction_ref(self, session_id: str) -> str | None:
		sale = self.find_paid_sale(session_id)
		return sale.id if sale else None

	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		"""Verify and ignore: card sales finish in `pay()`. Disputes are not fed back into orders yet."""
		form = parse_qs(payload.decode())
		signature = (form.get("bt_signature") or [None])[0]
		body = (form.get("bt_payload") or [None])[0]
		if not (signature and body):
			frappe.throw(_("Braintree webhook is missing bt_signature or bt_payload"))

		# Local HMAC with our keys, so it stays here and never goes through a stubbable client.
		# An invalid signature raises, which `webhook.handle` turns into its opaque 400.
		self.get_braintree_gateway().webhook_notification.parse(signature, body)
		return None

	def get_paid_sale(self, session_id: str) -> BraintreeTransaction:
		if sale := self.find_paid_sale(session_id):
			return sale

		frappe.throw(_("No successful Braintree payment found for this request"))

	def find_paid_sale(self, session_id: str) -> BraintreeTransaction | None:
		for transaction in self.get_client().search_sales(session_id):
			if transaction.status in BRAINTREE_PAID_STATUSES:
				return transaction
		return None


def acquire_session_lock(session_id: str):
	"""A MariaDB named lock: unlike a row lock it survives the commits `create_request_log` makes."""
	if not frappe.db.sql(
		"SELECT GET_LOCK(%s, %s)", (get_session_lock_name(session_id), SESSION_LOCK_TIMEOUT)
	)[0][0]:
		frappe.throw(_("Another payment attempt for this order is in progress. Please wait and retry."))


def release_session_lock(session_id: str):
	frappe.db.sql("SELECT RELEASE_LOCK(%s)", (get_session_lock_name(session_id),))


def get_session_lock_name(session_id: str) -> str:
	return f"braintree_session_{session_id}"


def format_amount(amount: float, currency: str) -> str:
	"""A decimal string, with no decimal point at all for zero-decimal currencies."""
	exponent = get_minor_unit_exponent(currency)
	return f"{flt(amount, exponent):.{exponent}f}"


def read_refund_status(refund: BraintreeTransaction) -> str:
	# A refund before settlement voids the sale itself, and a voided sale is a full refund.
	if refund.type == "sale":
		return "processed" if refund.status == "voided" else "pending"
	return BRAINTREE_REFUND_STATUS_MAP.get(refund.status, "pending")


def get_payable_request(session: str):
	"""The request behind a checkout session, refused unless its profile is an enabled Braintree one."""
	request = session and frappe.db.get_value(
		"Gateway Payment Request", {"order_ref": session}, ["name", "gateway"], as_dict=True
	)
	profile = request and frappe.db.get_value(
		"Payment Gateway Profile", request.gateway, ["gateway_settings", "enabled"], as_dict=True
	)
	if not (profile and profile.enabled and profile.gateway_settings == SETTINGS_DOCTYPE):
		frappe.throw(_("This payment link is not valid."), frappe.PermissionError)
	return frappe.get_doc("Gateway Payment Request", request.name)


# nosemgrep: guest-whitelisted-method  # the 32-character session token is the capability
@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=60, seconds=60, ip_based=True)
def start_checkout(session: str) -> Response:
	try:
		payment_request = get_payable_request(session)
	except frappe.PermissionError:
		frappe.clear_messages()
		return render_checkout({"error": _("This payment link is not valid.")}, status=404)

	if payment_request.status == "Paid":
		return redirect(payment_request.success_url)
	if payment_request.status != "Pending":
		return render_checkout(
			{"error": _("This payment is no longer open."), "cancel_url": payment_request.cancel_url}
		)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	currency = payment_request.currency_code
	return render_checkout(
		{
			"session": session,
			"amount": format_amount(payment_request.amount, currency),
			"currency": currency,
			"email": payment_request.customer_email or "",
			"three_d_secure": bool(settings.three_d_secure),
			"cancel_url": payment_request.cancel_url,
			"client_token": settings.get_client().generate_client_token(
				settings.get_merchant_account(currency)
			),
			# A signed-in shopper's POST to `pay()` is refused without it.
			"csrf_token": get_csrf_token(),
		}
	)


def render_checkout(context: dict, status: int = 200) -> Response:
	# nosemgrep: frappe-ssti  # CHECKOUT_TEMPLATE is a file in this app, never user input
	html = frappe.render_template(CHECKOUT_TEMPLATE, {"title": _("Pay by card"), **context})
	# The page holds the shopper's email and CSRF token.
	return Response(
		html, status=status, content_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"}
	)


# nosemgrep: guest-whitelisted-method  # the 32-character session token is the capability
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60, ip_based=True)
def pay(session: str, nonce: str) -> dict:
	"""Charge the request behind `session` once. The amount comes from the request, never the browser.

	Returns `{"redirect_url"}` once paid, or `{"error"}` for a decline the shopper can retry.
	"""
	get_payable_request(session)
	acquire_session_lock(session)
	try:
		# Nothing is written yet. A fresh snapshot after the wait, or MariaDB's snapshot isolation fails
		# the row lock below with "Record has changed" once the first attempt commits.
		frappe.db.commit()  # nosemgrep
		result = charge_request(get_payable_request(session), nonce)
		# Committed before the lock is released, so a second attempt sees this sale and does not charge.
		frappe.db.commit()  # nosemgrep
		return result
	finally:
		release_session_lock(session)


def charge_request(payment_request, nonce: str) -> dict:
	payment_request.lock_row()
	if payment_request.status == "Paid":
		return {"redirect_url": payment_request.success_url}
	if payment_request.status != "Pending":
		frappe.throw(_("This payment is no longer open."))

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	# A sale an earlier attempt made, if its order failed or the shopper retried before the page caught up.
	if not payment_request.gateway_transaction_ref and (
		settings.get_payment_status(payment_request.order_ref) != "Paid"
	):
		result = settings.charge(
			payment_request.order_ref, payment_request.amount, payment_request.currency_code, nonce
		)
		if not result.is_success:
			return {
				"error": _("The payment was declined: {0}. Please try another card.").format(result.message)
			}
		payment_request.db_set("gateway_transaction_ref", result.transaction.id, update_modified=False)
		# The money has moved: keep the record even if placing the order below fails.
		frappe.db.commit()  # nosemgrep

	# Placed here rather than by the return page, so closing the tab after paying still places the order.
	try:
		# Paid from the sale itself, since search may not show it yet. As the shopper, not Administrator:
		# the order's app elevates itself and stamps the shopper as owner.
		payment_request.lock_row()
		if payment_request.status == "Pending":
			payment_request.status = "Paid"
			payment_request.save(ignore_permissions=True)
	except Exception:
		# The shopper has paid, so they still go to the confirmation page, whose sync retries the order.
		frappe.db.rollback()
		frappe.log_error(title=f"Braintree order placement failed for {payment_request.name}")
	return {"redirect_url": payment_request.success_url}
