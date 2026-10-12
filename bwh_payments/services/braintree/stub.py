from datetime import datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import frappe
from frappe import _

from bwh_payments.services.braintree.base import BaseBraintreeClient, BraintreeResult, BraintreeTransaction

VALID_NONCE = "fake-valid-nonce"
# Braintree's sandbox declines any amount in this range, whatever the card.
DECLINED_AMOUNTS = (Decimal("2000.00"), Decimal("3000.00"))
REFUNDABLE_STATUSES = ("settled", "settling")
VOIDABLE_STATUSES = ("authorized", "submitted_for_settlement", "settlement_pending")


class StubBraintreeClient(BaseBraintreeClient):
	"""In-memory Braintree for tests. Call reset() in setUp."""

	transactions: ClassVar[dict[str, BraintreeTransaction]] = {}
	# Fresh sales Braintree's search index has not caught up with yet.
	unindexed: ClassVar[set[str]] = set()
	merchant_accounts: ClassVar[set[str]] = set()
	client_tokens: ClassVar[list[str]] = []
	sales: ClassVar[list[dict]] = []
	refunds: ClassVar[list[dict]] = []
	voids: ClassVar[list[str]] = []

	@classmethod
	def reset(cls):
		cls.transactions = {}
		cls.unindexed = set()
		cls.merchant_accounts = set()
		cls.client_tokens = []
		cls.sales = []
		cls.refunds = []
		cls.voids = []

	@classmethod
	def add_merchant_account(cls, merchant_account_id: str):
		cls.merchant_accounts.add(merchant_account_id)

	@classmethod
	def add_transaction(
		cls,
		order_id: str,
		amount: str = "10.00",
		status: str = "submitted_for_settlement",
		type: str = "sale",
		currency: str = "USD",
	) -> str:
		transaction_id = frappe.generate_hash(length=8)
		cls.transactions[transaction_id] = BraintreeTransaction(
			id=transaction_id,
			type=type,
			status=status,
			order_id=order_id,
			amount=amount,
			currency_iso_code=currency,
			# Strictly increasing, so "latest attempt" is well defined within one test.
			created_at=datetime(2026, 1, 1) + timedelta(seconds=len(cls.transactions)),
		)
		return transaction_id

	@classmethod
	def set_status(cls, transaction_id: str, status: str):
		cls.transactions[transaction_id].status = status

	@classmethod
	def settle(cls, transaction_id: str):
		cls.set_status(transaction_id, "settled")

	def generate_client_token(self, merchant_account_id: str) -> str:
		if merchant_account_id not in self.merchant_accounts:
			frappe.throw(
				_("Braintree refused merchant account {0}: {1}").format(
					merchant_account_id, "Merchant account not found"
				)
			)
		self.client_tokens.append(merchant_account_id)
		return f"stub-client-token-{merchant_account_id}"

	def sale(self, params: dict) -> BraintreeResult:
		self.sales.append(params)
		amount = Decimal(params["amount"])
		if params.get("payment_method_nonce") != VALID_NONCE:
			return BraintreeResult(is_success=False, message="Unknown or expired payment_method_nonce.")

		low, high = DECLINED_AMOUNTS
		declined = low <= amount < high
		submit = (params.get("options") or {}).get("submit_for_settlement")
		status = "processor_declined" if declined else "submitted_for_settlement" if submit else "authorized"
		transaction_id = self.add_transaction(
			order_id=params.get("order_id"), amount=params["amount"], status=status
		)
		self.unindexed.add(transaction_id)
		return BraintreeResult(
			is_success=not declined,
			message="Do Not Honor" if declined else None,
			transaction=self.find_transaction(transaction_id),
		)

	def find_transaction(self, transaction_id: str) -> BraintreeTransaction:
		if transaction_id not in self.transactions:
			frappe.throw(_("Braintree rejected the request: {0}").format("NotFoundError"))
		return self.transactions[transaction_id].model_copy(deep=True)

	def search_sales(self, order_id: str) -> list[BraintreeTransaction]:
		return [
			transaction.model_copy(deep=True)
			for transaction in self.transactions.values()
			if transaction.order_id == order_id
			and transaction.type == "sale"
			and transaction.id not in self.unindexed
		]

	def refund(self, transaction_id: str, amount: str) -> BraintreeResult:
		self.refunds.append({"transaction_id": transaction_id, "amount": amount})
		sale = self.find_transaction(transaction_id)
		if sale.status not in REFUNDABLE_STATUSES:
			return BraintreeResult(
				is_success=False, message="Cannot refund transaction unless it is settled."
			)

		refunded = sum(
			Decimal(transaction.amount)
			for transaction in self.transactions.values()
			if transaction.type == "credit" and transaction.order_id == sale.order_id
		)
		if refunded + Decimal(amount) > Decimal(sale.amount):
			return BraintreeResult(is_success=False, message="Refund amount is too large.")

		credit_id = self.add_transaction(
			order_id=sale.order_id, amount=amount, type="credit", currency=sale.currency_iso_code
		)
		return BraintreeResult(is_success=True, transaction=self.find_transaction(credit_id))

	def void(self, transaction_id: str) -> BraintreeResult:
		self.voids.append(transaction_id)
		sale = self.find_transaction(transaction_id)
		if sale.status not in VOIDABLE_STATUSES:
			return BraintreeResult(
				is_success=False,
				message="Transaction can only be voided if status is authorized or submitted_for_settlement.",
			)

		self.set_status(transaction_id, "voided")
		return BraintreeResult(is_success=True, transaction=self.find_transaction(transaction_id))
