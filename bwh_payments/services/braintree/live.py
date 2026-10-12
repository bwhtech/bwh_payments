import braintree
import frappe
from braintree.exceptions.braintree_error import BraintreeError
from frappe import _
from frappe.integrations.utils import create_request_log

from bwh_payments.services.braintree.base import BaseBraintreeClient, BraintreeResult, BraintreeTransaction

SETTINGS_DOCTYPE = "Braintree Gateway Settings"


class LiveBraintreeClient(BaseBraintreeClient):
	def __init__(self, gateway: braintree.BraintreeGateway):
		self.gateway = gateway

	def generate_client_token(self, merchant_account_id: str) -> str:
		endpoint = "client_token.generate"
		try:
			token = self.gateway.client_token.generate({"merchant_account_id": merchant_account_id})
		except BraintreeError:
			# AuthenticationError and friends carry no message; the keys are the only likely cause.
			log_request(endpoint, error="credentials rejected")
			frappe.throw(_("Braintree rejected the API credentials. Check the Mode and the keys."))
		except ValueError as exception:
			log_request(endpoint, error=str(exception))
			frappe.throw(
				_("Braintree refused merchant account {0}: {1}").format(merchant_account_id, str(exception))
			)
		return token

	def sale(self, params: dict) -> BraintreeResult:
		return self.call("transaction.sale", lambda: self.gateway.transaction.sale(params))

	def find_transaction(self, transaction_id: str) -> BraintreeTransaction:
		return parse_transaction(
			self.call_sdk("transaction.find", lambda: self.gateway.transaction.find(transaction_id))
		)

	def search_sales(self, order_id: str) -> list[BraintreeTransaction]:
		collection = self.call_sdk(
			"transaction.search",
			lambda: self.gateway.transaction.search(
				braintree.TransactionSearch.order_id == order_id,
				braintree.TransactionSearch.type == braintree.Transaction.Type.Sale,
			),
		)
		return [parse_transaction(transaction) for transaction in collection.items]

	def refund(self, transaction_id: str, amount: str) -> BraintreeResult:
		return self.call(
			"transaction.refund", lambda: self.gateway.transaction.refund(transaction_id, amount)
		)

	def void(self, transaction_id: str) -> BraintreeResult:
		return self.call("transaction.void", lambda: self.gateway.transaction.void(transaction_id))

	def call(self, endpoint: str, request) -> BraintreeResult:
		result = BraintreeResult.model_validate(self.call_sdk(endpoint, request), from_attributes=True)
		transaction = result.transaction
		log_request(
			endpoint,
			output={
				"is_success": result.is_success,
				"transaction_id": transaction.id if transaction else None,
				"status": transaction.status if transaction else None,
			},
		)
		return result

	def call_sdk(self, endpoint: str, request):
		try:
			return request()
		except BraintreeError as exception:
			# Most SDK exceptions carry no message, so the class name is what tells them apart.
			description = str(exception) or exception.__class__.__name__
			log_request(endpoint, error=description)
			frappe.throw(_("Braintree rejected the request: {0}").format(description))


def parse_transaction(transaction) -> BraintreeTransaction:
	return BraintreeTransaction.model_validate(transaction, from_attributes=True)


def log_request(endpoint: str, output=None, error=None):
	# Only the endpoint and the outcome: never the nonce, card details or the shopper's details.
	create_request_log(
		{"endpoint": endpoint},
		service_name="Braintree",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)
