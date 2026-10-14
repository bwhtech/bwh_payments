import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_post_request

from bwh_payments.services.payu.base import BasePayUClient, PayURefund, PayUTransaction, payu_hash

PAYU_TEST_URL = "https://test.payu.in/merchant/postservice.php?form=2"
PAYU_LIVE_URL = "https://info.payu.in/merchant/postservice.php?form=2"
SETTINGS_DOCTYPE = "PayU Gateway Settings"


class LivePayUClient(BasePayUClient):
	def __init__(self, key: str, salt: str, test_mode: bool):
		self.key = key
		self.salt = salt
		self.url = PAYU_TEST_URL if test_mode else PAYU_LIVE_URL

	def verify_payment(self, txnid: str) -> PayUTransaction:
		response = self.post_command("verify_payment", txnid)
		return PayUTransaction.model_validate(get_details(response, txnid))

	def refund(self, mihpayid: str, token: str, amount: str) -> PayURefund:
		response = self.post_command("cancel_refund_transaction", mihpayid, token, amount)
		return PayURefund.model_validate({"mihpayid": mihpayid, "amt": amount, **response})

	def get_refund(self, request_id: str) -> PayURefund:
		details = get_details(self.post_command("check_action_status", request_id), request_id)
		# Read the entry flat, or nested a second time under its request id.
		details = details.get(request_id) or details
		return PayURefund.model_validate({"request_id": request_id, **details})

	def post_command(self, command: str, *variables: str) -> dict:
		# Only var1 is hashed: sha512(key|command|var1|salt).
		data = {
			"key": self.key,
			"command": command,
			"hash": payu_hash(self.key, command, variables[0], self.salt),
		}
		data.update({f"var{index}": value for index, value in enumerate(variables, start=1)})

		try:
			response = make_post_request(self.url, data=data)
		except Exception as exception:
			log_request(command, error=str(exception))
			frappe.throw(_("PayU rejected the request: {0}").format(_("unknown error")))

		if not isinstance(response, dict):
			log_request(command, error="PayU returned a non-JSON body")
			frappe.throw(_("PayU returned an unreadable response"))

		# verify_payment answers status 0 for an unknown txnid but still carries its details.
		if str(response.get("status")) == "0" and not response.get("transaction_details"):
			log_request(command, error=response.get("msg"))
			frappe.throw(_("PayU rejected the request: {0}").format(response.get("msg")))

		log_request(command, output={"status": response.get("status"), "msg": response.get("msg")})
		return response


def get_details(response: dict, key: str) -> dict:
	return (response.get("transaction_details") or {}).get(key) or {}


def log_request(command: str, output=None, error=None):
	# The form carries the merchant key and hash, so only the endpoint and command are logged.
	create_request_log(
		{"endpoint": "postservice.php", "command": command},
		service_name="PayU",
		is_remote_request=True,
		reference_doctype=SETTINGS_DOCTYPE,
		reference_docname=SETTINGS_DOCTYPE,
		output=output,
		error=error,
		status="Failed" if error else "Completed",
	)
