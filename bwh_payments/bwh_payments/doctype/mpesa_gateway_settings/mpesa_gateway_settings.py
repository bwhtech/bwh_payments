# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import hmac
import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.data import cint, flt

from bwh_payments.base_class import CheckoutSession, PaymentGatewayBase, RefundResult, WebhookEvent
from bwh_payments.bwh_payments.utils import add_reference_id, get_localised_url
from bwh_payments.currency import validate_transaction_currency
from bwh_payments.services.mpesa.base import BaseMpesaClient
from bwh_payments.services.mpesa.live import LiveMpesaClient
from bwh_payments.services.mpesa.stub import StubMpesaClient

MPESA_CURRENCY = "KES"
WEBHOOK_ENDPOINT = "/api/method/bwh_payments.bwh_payments.webhook.handle"

# STK result codes. Unmapped codes stay Pending.
MPESA_RESULT_STATUS_MAP = {
	"0": "Paid",
	"1": "Not Paid",  # insufficient balance
	"11": "Not Paid",  # the number cannot pay (DebitParty in an invalid state)
	"1001": "Not Paid",  # another transaction is in progress for this subscriber
	"1019": "Expired",
	"1025": "Not Paid",  # the prompt could not be sent
	"1032": "Cancelled",  # the shopper dismissed the prompt
	"1037": "Expired",  # the shopper's phone did not answer
	"2001": "Not Paid",  # wrong PIN
	"9999": "Not Paid",  # the prompt could not be sent
}
MPESA_SUCCESS_RESULT_CODE = "0"
MPESA_RECEIPT_ITEM = "MpesaReceiptNumber"
MPESA_PAY_BILL = "Pay Bill"
# Daraja caps these and rejects longer values.
MPESA_ACCOUNT_REFERENCE_LENGTH = 12
MPESA_TRANSACTION_DESC = "Order payment"
# The value Daraja documents for a reversal; the misspelt key is Daraja's own.
MPESA_REVERSAL_RECEIVER_TYPE = "11"
KENYAN_MOBILE_NUMBER = re.compile(r"254[17]\d{8}")


class MpesaGatewaySettings(Document, PaymentGatewayBase):
	logo = "/assets/bwh_payments/images/gateways/mpesa.svg"
	description = "Mobile money through a PIN prompt on the shopper's phone. Kenya, KES only."
	docs_url = "https://developer.safaricom.co.ke/APIs/MpesaExpressSimulate"

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		business_shortcode: DF.Data
		callback_token: DF.Password | None
		cancelled_url: DF.Data | None
		consumer_key: DF.Data
		consumer_secret: DF.Password
		enabled: DF.Check
		failure_url: DF.Data | None
		initiator_name: DF.Data | None
		online_passkey: DF.Password
		sandbox: DF.Check
		security_credential: DF.Password | None
		success_url: DF.Data | None
		till_number: DF.Data | None
		transaction_type: DF.Literal["Pay Bill", "Buy Goods"]
		webhook_ips: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		if not self.callback_token:
			self.callback_token = frappe.generate_hash(length=32)

	def get_gateway_name(self) -> str:
		return "M-Pesa"

	def get_client(self) -> BaseMpesaClient:
		if frappe.in_test:
			return StubMpesaClient()

		# A stray space from pasting the keys reaches Daraja only as "Wrong credentials".
		return LiveMpesaClient(
			self.consumer_key.strip(),
			self.get_password("consumer_secret").strip(),
			self.business_shortcode.strip(),
			self.get_password("online_passkey").strip(),
			bool(self.sandbox),
			self.initiator_name,
			self.get_password("security_credential", raise_exception=False),
		)

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
		return_url: str | None = None,
	) -> CheckoutSession:
		currency = currency or MPESA_CURRENCY
		validate_transaction_currency(currency)
		validate_mpesa_currency(currency)

		phone_number = get_mpesa_phone_number((customer or {}).get("phone"))
		pay_bill = self.transaction_type == MPESA_PAY_BILL
		payload = {
			"TransactionType": "CustomerPayBillOnline" if pay_bill else "CustomerBuyGoodsOnline",
			"Amount": get_whole_shillings(amount),
			"PartyA": phone_number,
			"PartyB": self.business_shortcode if pay_bill else self.till_number,
			"PhoneNumber": phone_number,
			"CallBackURL": self.get_callback_url(),
			"AccountReference": (reference or self.business_shortcode)[-MPESA_ACCOUNT_REFERENCE_LENGTH:],
			"TransactionDesc": MPESA_TRANSACTION_DESC,
		}

		push = self.get_client().send_stk_push(payload)
		# The prompt goes to the shopper's phone, so there is no checkout page to send them to. Every URL
		# is the return page, which shows the payment as Pending until the shopper enters their PIN.
		success_url = return_url or self.build_success_url(reference)
		return CheckoutSession(
			session_id=push.checkout_request_id,
			redirect_url=success_url,
			success_url=success_url,
			cancel_url=return_url or get_localised_url(self.cancelled_url),
			failure_url=return_url or get_localised_url(self.failure_url),
		)

	def build_success_url(self, reference: str | None) -> str:
		if not self.success_url:
			frappe.throw(_("Please set the Success URL in Mpesa Gateway Settings"))
		return add_reference_id(self.success_url, reference)

	def get_callback_url(self) -> str:
		"""Daraja takes a callback URL per request and cannot sign it, so the URL carries a secret token."""
		gateway = frappe.db.get_value(
			"Payment Gateway Profile", {"gateway_settings": self.doctype, "enabled": 1}, "name"
		)
		if not gateway:
			frappe.throw(_("No enabled Payment Gateway Profile uses Mpesa Gateway Settings"))
		query = urlencode({"gateway": gateway, "token": self.get_password("callback_token")})
		return frappe.utils.get_url(f"{WEBHOOK_ENDPOINT}?{query}")

	def get_payment_status(self, session_id: str) -> str:
		return read_result_status(self.get_client().query_stk_push(session_id).result_code)

	def handle_webhook(self, payload: bytes, headers: dict) -> WebhookEvent | None:
		self.validate_webhook_source_ip()

		callback_token = self.get_password("callback_token", raise_exception=False)
		if not callback_token:
			frappe.throw(_("Callback token is not configured in Mpesa Gateway Settings"))

		# Daraja does not sign callbacks; the token in the callback URL is the shared secret.
		token = frappe.request.args.get("token") or ""
		# compare_digest raises TypeError on a non-ASCII str.
		if not token.isascii() or not hmac.compare_digest(callback_token, token):
			frappe.throw(_("M-Pesa callback token does not match"))

		# parse_json passes bytes through untouched. Decode and JSON errors are both ValueError.
		try:
			body = frappe.parse_json(payload.decode())
		except ValueError:
			return None
		if not isinstance(body, dict):
			return None

		if result := body.get("Result"):
			return read_reversal_result(result)
		if callback := (body.get("Body") or {}).get("stkCallback"):
			return read_stk_callback(callback)
		return None

	def validate_webhook_source_ip(self):
		"""Defence in depth on top of the callback token."""
		allowed_ips = [line.strip() for line in (self.webhook_ips or "").splitlines() if line.strip()]
		if not allowed_ips:
			return
		if frappe.local.request_ip not in allowed_ips:
			frappe.throw(_("M-Pesa callback came from an address that is not allowed"))

	def supported_currencies(self) -> tuple[str, ...]:
		return (MPESA_CURRENCY,)

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> RefundResult:
		validate_mpesa_currency(currency or MPESA_CURRENCY)

		# Only the STK callback names the M-Pesa receipt; Daraja cannot be asked for it later.
		receipt = frappe.db.get_value(
			"Gateway Payment Request", {"order_ref": session_id}, "gateway_transaction_ref"
		)
		if not receipt:
			frappe.throw(
				_(
					"M-Pesa never sent the receipt number for this payment, so it cannot be reversed here."
					" Reverse it from the M-PESA Org Portal."
				)
			)

		callback_url = self.get_callback_url()
		reversal = self.get_client().reverse_transaction(
			{
				"CommandID": "TransactionReversal",
				"TransactionID": receipt,
				"Amount": get_whole_shillings(amount),
				"ReceiverParty": self.business_shortcode,
				"RecieverIdentifierType": MPESA_REVERSAL_RECEIVER_TYPE,
				"ResultURL": callback_url,
				"QueueTimeOutURL": callback_url,
				"Remarks": "Refund",
				"Occasion": session_id,
			}
		)
		# Safaricom only queues a reversal; its result arrives as a callback.
		return RefundResult(refund_id=reversal.conversation_id, status="pending", amount=flt(amount))


def validate_mpesa_currency(currency: str):
	if (currency or "").strip().upper() != MPESA_CURRENCY:
		frappe.throw(
			_("M-Pesa cannot take payment in {0}. It supports {1}.").format(
				frappe.bold(currency), MPESA_CURRENCY
			)
		)


def get_whole_shillings(amount: float) -> int:
	"""Daraja takes whole shillings only. Rounding would charge a different amount, so cents are refused."""
	shillings = cint(flt(amount, 2))
	if flt(amount, 2) != shillings:
		frappe.throw(_("M-Pesa takes whole shillings only, not {0}").format(flt(amount, 2)))
	return shillings


def get_mpesa_phone_number(phone: str | None) -> int:
	"""Daraja takes 2547XXXXXXXX or 2541XXXXXXXX."""
	digits = re.sub(r"\D", "", phone or "")
	if digits.startswith("0"):
		digits = f"254{digits[1:]}"
	elif len(digits) == 9:
		digits = f"254{digits}"

	if not KENYAN_MOBILE_NUMBER.fullmatch(digits):
		frappe.throw(_("M-Pesa needs the shopper's Kenyan mobile number, such as 0712 345 678"))
	return int(digits)


def read_result_status(result_code, is_final: bool = False) -> str:
	if result_code is None:
		return "Pending"
	# A callback comes only once the prompt is over, so any code it carries is final. A query can
	# still answer with an unlisted code while the shopper is entering their PIN (4999).
	return MPESA_RESULT_STATUS_MAP.get(str(result_code).strip(), "Not Paid" if is_final else "Pending")


def read_stk_callback(callback: dict) -> WebhookEvent:
	session_id = callback.get("CheckoutRequestID")
	if not session_id:
		frappe.throw(_("M-Pesa callback carried no CheckoutRequestID"))

	result_code = str(callback.get("ResultCode", "")).strip()
	receipt = None
	if result_code == MPESA_SUCCESS_RESULT_CODE:
		receipt = get_callback_item(callback, MPESA_RECEIPT_ITEM)
		# A paid callback is the only place the receipt appears, and refunds need it.
		if not receipt:
			frappe.throw(_("M-Pesa paid callback carried no receipt number"))

	return WebhookEvent(
		session_id=session_id,
		status=read_result_status(result_code, is_final=True),
		# Safaricom sends one callback per prompt.
		event_id=session_id,
		transaction_ref=receipt,
	)


def get_callback_item(callback: dict, name: str) -> str | None:
	for item in (callback.get("CallbackMetadata") or {}).get("Item") or []:
		if item.get("Name") == name:
			return item.get("Value")
	return None


def read_reversal_result(result: dict) -> WebhookEvent:
	refund_id = result.get("ConversationID")
	if not refund_id:
		frappe.throw(_("M-Pesa reversal result carried no ConversationID"))

	succeeded = str(result.get("ResultCode", "")).strip() == MPESA_SUCCESS_RESULT_CODE
	return WebhookEvent(
		refund_id=refund_id,
		status="processed" if succeeded else "failed",
		event_id=refund_id,
	)
