# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import braintree
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url
from frappe.utils.data import flt

from bwh_payments.base_class import (
	REUSABLE_CHECKOUT,
	CheckoutSession,
	PaymentGatewayBase,
	RefundResult,
	WebhookEvent,
)
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency
from bwh_payments.services.braintree.base import BaseBraintreeClient, BraintreeResult, BraintreeTransaction
from bwh_payments.services.braintree.live import LiveBraintreeClient
from bwh_payments.services.braintree.stub import StubBraintreeClient

BRAINTREE_ENVIRONMENTS = {
	"Sandbox": braintree.Environment.Sandbox,
	"Production": braintree.Environment.Production,
}

# Braintree has no hosted payment page, so the shopper is sent to ours, which renders Drop-in.
CHECKOUT_PAGE_ROUTE = "/braintree-checkout"
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

# Unmapped states stay Pending. Declines are Pending too, so the shopper can retry another card.
BRAINTREE_STATUS_MAP = {
	**dict.fromkeys(BRAINTREE_PAID_STATUSES, "Paid"),
	"authorizing": "Pending",
	"authorized": "Pending",
	"processor_declined": "Pending",
	"gateway_rejected": "Pending",
	"failed": "Pending",
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
	capabilities = (REUSABLE_CHECKOUT,)

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
			redirect_url=get_url(f"{CHECKOUT_PAGE_ROUTE}?{urlencode({'session': session_id})}"),
			success_url=return_url or self.build_return_url(self.success_url, _("Success URL"), reference),
			cancel_url=return_url or self.build_return_url(self.cancelled_url, _("Cancelled URL"), reference),
			failure_url=return_url or get_localised_url(self.failure_url),
		)

	def build_return_url(self, url: str, label: str, reference: str | None) -> str:
		"""The storefront's confirmation page reads `reference_id`."""
		if not url:
			frappe.throw(_("Please set the {0} in Braintree Gateway Settings").format(label))
		parts = urlsplit(get_localised_url(url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def charge(self, session_id: str, amount: float, currency: str, nonce: str) -> BraintreeResult:
		"""One sale for the request's own amount; the caller holds the session lock."""
		return self.get_client().sale(
			{
				"amount": format_amount(amount, currency),
				"payment_method_nonce": nonce,
				"merchant_account_id": self.get_merchant_account(currency),
				"order_id": session_id,
				"options": {"submit_for_settlement": True},
			}
		)

	def get_payment_status(self, session_id: str) -> str:
		transactions = self.get_transactions(session_id)
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
		cancellable = not any(
			transaction.status in BRAINTREE_UNCANCELLABLE_STATUSES
			for transaction in self.get_transactions(session_id)
		)
		if not cancellable:
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
		if not refund:
			return RefundResult(amount=flt(amount))
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
		for transaction in self.get_transactions(session_id):
			if transaction.status in BRAINTREE_PAID_STATUSES:
				return transaction
		return None

	def get_transactions(self, session_id: str) -> list[BraintreeTransaction]:
		"""Every sale on this session, declines included.

		Search can trail a fresh sale by a few seconds, so the sale `pay()` recorded is also fetched directly.
		"""
		client = self.get_client()
		transactions = {transaction.id: transaction for transaction in client.search_sales(session_id)}

		known_id = frappe.db.get_value(
			"Gateway Payment Request", {"order_ref": session_id}, "gateway_transaction_ref"
		)
		if known_id and known_id not in transactions:
			transactions[known_id] = client.find_transaction(known_id)

		return list(transactions.values())


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
