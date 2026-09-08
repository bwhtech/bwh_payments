# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import base64
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.model.document import Document
from frappe.utils.data import cint, flt

from bwh_payments.base_class import PaymentGatewayBase
from bwh_payments.bwh_payments.utils import get_localised_url
from bwh_payments.currency import get_minor_unit_exponent, validate_transaction_currency

# ponytail: frappe.integrations.utils.make_request takes no timeout, so a hung PayPal call holds a
# worker; revisit if PayPal latency ever shows up in the request log.
PAYPAL_BASE_URLS = {
	"Sandbox": "https://api-m.sandbox.paypal.com",
	"Live": "https://api-m.paypal.com",
}

# PayPal settles in these and nothing else. Notably absent are SAR, AED, KWD, BHD, QAR and INR, so a
# Gulf storefront cannot offer PayPal on its home currency at all — which is why the contract grew
# `get_supported_currencies` rather than letting the shopper find out at the end of checkout.
PAYPAL_SUPPORTED_CURRENCIES = (
	"AUD",
	"CAD",
	"CHF",
	"CZK",
	"DKK",
	"EUR",
	"GBP",
	"HKD",
	"HUF",
	"ILS",
	"JPY",
	"MXN",
	"MYR",
	"NOK",
	"NZD",
	"PHP",
	"PLN",
	"SEK",
	"SGD",
	"THB",
	"TWD",
	"USD",
)

# PayPal rejects a decimal point in these outright, whatever ISO 4217 says. JPY is already zero-decimal
# in `currency.MINOR_UNIT_EXPONENTS`, but HUF and TWD are two-decimal currencies everywhere else, so
# without this override every HUF charge is sent as "1000.00" and refused.
PAYPAL_WHOLE_AMOUNT_CURRENCIES = ("HUF", "JPY", "TWD")

# PayPal reports order status in uppercase. Anything unrecognised stays Pending: never terminal, and
# never Paid, so an unmapped PayPal state can neither release goods nor cancel a live order. `APPROVED`
# is deliberately absent — the shopper has authorised the charge but no money has moved, and that is
# resolved by capturing.
PAYPAL_STATUS_MAP = {
	"COMPLETED": "Paid",
	"VOIDED": "Cancelled",
	"CREATED": "Pending",
	"SAVED": "Pending",
	"PAYER_ACTION_REQUIRED": "Pending",
}

PAYPAL_APPROVED_STATUS = "APPROVED"
PAYPAL_COMPLETED_STATUS = "COMPLETED"
PAYPAL_ACCEPTED_REFUND_STATUSES = ("COMPLETED", "PENDING")
PAYPAL_ALREADY_CAPTURED_ISSUE = "ORDER_ALREADY_CAPTURED"
PAYPAL_VERIFICATION_SUCCESS = "SUCCESS"

PAYPAL_ORDER_APPROVED_EVENT = "CHECKOUT.ORDER.APPROVED"
PAYPAL_CAPTURE_COMPLETED_EVENT = "PAYMENT.CAPTURE.COMPLETED"

# The link PayPal wants the shopper sent to. `payer-action` is what an order created with a
# `payment_source` answers with; `approve` is the older spelling, kept as a fallback.
PAYPAL_REDIRECT_LINK_RELS = ("payer-action", "approve")

# `dict(frappe.request.headers)` loses werkzeug's case-insensitivity, so these have to match the
# canonical title-case werkzeug hands over, not PayPal's documented all-caps spelling. Keys are the
# field names PayPal's verification endpoint expects.
PAYPAL_SIGNATURE_HEADERS = {
	"auth_algo": "Paypal-Auth-Algo",
	"cert_url": "Paypal-Cert-Url",
	"transmission_id": "Paypal-Transmission-Id",
	"transmission_sig": "Paypal-Transmission-Sig",
	"transmission_time": "Paypal-Transmission-Time",
}


class PayPalGatewaySettings(Document, PaymentGatewayBase):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		brand_name: DF.Data | None
		cancelled_url: DF.Data | None
		client_id: DF.Data
		client_secret: DF.Password
		currency: DF.Link | None
		enabled: DF.Check
		failure_url: DF.Data | None
		mode: DF.Literal["Sandbox", "Live"]
		success_url: DF.Data | None
		webhook_id: DF.Data
	# end: auto-generated types

	def get_gateway_name(self) -> str:
		return "PayPal"

	def get_supported_currencies(self) -> tuple[str, ...]:
		# `validate_paypal_currency` still enforces this on the way in: filtering only decides what the
		# checkout page offers, and the storefront resolves a shopper's chosen mode without a currency.
		return PAYPAL_SUPPORTED_CURRENCIES

	def get_base_url(self) -> str:
		return PAYPAL_BASE_URLS[self.mode or "Sandbox"]

	def get_access_token(self) -> str:
		"""A client-credentials bearer token, cached until shortly before PayPal expires it.

		Every call below needs one, webhook verification on the guest callback path included, so fetching
		a fresh token per call would double every round trip the shopper waits on.
		"""
		# `frappe.cache` prefixes keys with the site itself; the mode is what keeps a sandbox token from
		# ever being presented to the live API.
		cache_key = f"paypal_access_token::{self.mode}"
		if token := frappe.cache.get_value(cache_key):
			return token

		url = f"{self.get_base_url()}/v1/oauth2/token"
		credentials = f"{self.client_id}:{self.get_password('client_secret')}"
		headers = {
			"Authorization": f"Basic {base64.b64encode(credentials.encode()).decode()}",
			"Content-Type": "application/x-www-form-urlencoded",
		}
		try:
			response = make_post_request(url, headers=headers, data="grant_type=client_credentials")
		except Exception as exception:
			self.throw_request_error(url, exception)
			# throw_request_error always raises today; the bare raise keeps `response` from ever being
			# read unbound if that stops being true.
			raise

		token = (response or {}).get("access_token")
		if not token:
			self.log_request(url, error="PayPal returned no access token")
			frappe.throw(_("PayPal did not return an access token"))

		# Expire early: a token that lapses between this read and the call it authorises surfaces as an
		# unexplained 401 in the middle of a checkout.
		ttl = max(cint(response.get("expires_in")) - 300, 60)
		frappe.cache.set_value(cache_key, token, expires_in_sec=ttl)
		return token

	def get_headers(self, request_id: str | None = None) -> dict:
		headers = {
			"Authorization": f"Bearer {self.get_access_token()}",
			"Content-Type": "application/json",
		}
		if request_id:
			# PayPal replays the original outcome for a request id it has already seen rather than acting
			# twice. That is what makes the confirmation page and the webhook safe to race on capture.
			headers["PayPal-Request-Id"] = request_id
		return headers

	def create_session(
		self,
		amount: float,
		currency: str,
		reference: str | None = None,
		customer: dict | None = None,
	) -> dict:
		currency = currency or self.currency
		validate_transaction_currency(currency)
		validate_paypal_currency(currency)

		success_url = self.build_return_url(self.success_url, _("Success URL"), reference)
		cancel_url = self.build_return_url(self.cancelled_url, _("Cancelled URL"), reference)
		payload = {
			"intent": "CAPTURE",
			"purchase_units": [
				{
					# Echoed on every capture and refund, so a PayPal statement reconciles back to a
					# request without us holding a second mapping of our own.
					"custom_id": reference,
					"amount": {
						"currency_code": currency.upper(),
						"value": format_paypal_amount(amount, currency),
					},
				}
			],
			"payment_source": {
				"paypal": {
					"experience_context": {
						# ponytail: `locale` goes unsent because PayPal wants BCP-47 ("en-US") while
						# `frappe.local.lang` is a bare language tag, and an unrecognised locale is a hard
						# 400. PayPal detects one from the shopper's own account meanwhile.
						"user_action": "PAY_NOW",
						"return_url": success_url,
						"cancel_url": cancel_url,
						**({"brand_name": self.brand_name} if self.brand_name else {}),
					}
				}
			},
		}
		# ponytail: no payer details are prefilled. PayPal restricts the wallet flow to the address given in
		# `payment_source.paypal.email_address`, and a shopper's storefront email is routinely not the one
		# their PayPal account uses — prefilling it locks those shoppers out of paying at all, which is a
		# far worse trade than saving them a login. `custom_id` above is what ties the order back to us.

		# The order id, not a capture id: `order_ref` holds this and `webhook.handle` matches it
		# byte-for-byte, so every webhook branch below has to resolve back to the same value.
		order = self.post("/v2/checkout/orders", payload, request_id=reference)
		return {
			"session_id": order["id"],
			"redirect_url": get_redirect_url(order),
			"success_url": success_url,
			"cancel_url": cancel_url,
			"failure_url": get_localised_url(self.failure_url),
		}

	def build_return_url(self, url: str, label: str, reference: str | None) -> str:
		"""PayPal only appends `?token=...&PayerID=...` on return, so the shopper carries our name back.

		The confirmation page reads `reference_id`; PayPal's `token` happens to be the order id and would
		resolve too, but nothing on the storefront looks at it.
		"""
		if not url:
			frappe.throw(_("Please set the {0} in PayPal Gateway Settings").format(label))
		# String-concatenating "?reference_id=..." breaks any URL that already carries a query.
		parts = urlsplit(get_localised_url(url))
		query = parse_qsl(parts.query)
		query.append(("reference_id", reference or ""))
		return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

	def get_order(self, session_id: str) -> dict:
		return self.get_resource(f"/v2/checkout/orders/{session_id}")

	def get_payment_status(self, session_id: str) -> str:
		order = self.get_order(session_id)
		if read_status(order) == PAYPAL_APPROVED_STATUS:
			order = self.capture_order(session_id)

		return PAYPAL_STATUS_MAP.get(read_status(order), "Pending")

	def capture_order(self, session_id: str) -> dict:
		"""Take the money the shopper approved, and return the order as it stands afterwards.

		Approval is permission, not payment: PayPal tells the shopper they have paid and then waits for
		this call, so without it the order sits approved forever and no money ever arrives. A failed
		capture raises, leaving the request Pending rather than reporting a charge that never happened.
		"""
		url = f"{self.get_base_url()}/v2/checkout/orders/{session_id}/capture"
		try:
			response = make_post_request(url, json={}, headers=self.get_headers(request_id=session_id))
		except Exception as exception:
			# The confirmation page and the CHECKOUT.ORDER.APPROVED webhook both capture, and on an
			# ordinary checkout they race. PayPal answers the loser with ORDER_ALREADY_CAPTURED, which
			# means the other call took the money — a success, not a failure.
			if read_paypal_issue(exception) != PAYPAL_ALREADY_CAPTURED_ISSUE:
				self.throw_request_error(url, exception)
				# throw_request_error always raises today; the bare raise keeps this from silently
				# falling through to the re-read if that stops being true.
				raise
			self.log_request(url, output={"issue": PAYPAL_ALREADY_CAPTURED_ISSUE})
		else:
			self.read_response(url, response)

		# Re-read rather than trusting the capture reply: on the already-captured path there is no reply
		# to trust, and both paths then map their status through exactly the same code.
		return self.get_order(session_id)

	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		event = self.verify_webhook(payload, headers)
		event_type = (event.get("event_type") or "").strip().upper()
		resource = event.get("resource") or {}

		if event_type == PAYPAL_ORDER_APPROVED_EVENT:
			session_id = resource.get("id")
			if not session_id:
				frappe.throw(_("PayPal sent a {0} event with no order id").format(event_type))
			# This is what charges a shopper who approved on PayPal and never came back to the storefront.
			status = PAYPAL_STATUS_MAP.get(read_status(self.capture_order(session_id)), "Pending")
		elif event_type == PAYPAL_CAPTURE_COMPLETED_EVENT:
			# `resource.id` here is the capture, not the order. The order id — the one `order_ref` holds —
			# is only carried in supplementary data, and reading the wrong one matches no request at all.
			related_ids = (resource.get("supplementary_data") or {}).get("related_ids") or {}
			session_id = related_ids.get("order_id")
			if not session_id:
				frappe.throw(_("PayPal sent a {0} event with no order id").format(event_type))
			status = "Paid"
		else:
			return {}

		return {"session_id": session_id, "status": status, "event_id": event.get("id")}

	def verify_webhook(self, payload: bytes, headers: dict) -> dict:
		"""Ask PayPal whether it really sent this body, and return the event once it says yes.

		PayPal signs with a certificate rather than a shared secret, so unlike every other gateway here
		there is nothing to verify locally — the check is a call back to PayPal, which costs this delivery
		a second round trip on top of the token.
		"""
		if not self.webhook_id:
			frappe.throw(_("Webhook ID is not configured in PayPal Gateway Settings"))

		transmission = {}
		for field, header in PAYPAL_SIGNATURE_HEADERS.items():
			value = headers.get(header)
			if not value:
				frappe.throw(_("Missing {0} header").format(header))
			transmission[field] = value

		# parse_json passes bytes straight through untouched, so the body is decoded first.
		# UnicodeDecodeError is a ValueError, and so is the JSONDecodeError parse_json raises on a body
		# it cannot read. Either way this is not a delivery we can verify.
		try:
			event = frappe.parse_json(payload.decode())
		except ValueError:
			frappe.throw(_("PayPal webhook body could not be read"))
		if not isinstance(event, dict):
			frappe.throw(_("PayPal webhook body was not an event"))

		verification = self.post(
			"/v1/notifications/verify-webhook-signature",
			{**transmission, "webhook_id": self.webhook_id, "webhook_event": event},
		)
		status = (verification.get("verification_status") or "").strip().upper()
		if status != PAYPAL_VERIFICATION_SUCCESS:
			frappe.throw(_("PayPal could not verify this webhook delivery"))

		return event

	def refund_payment(self, session_id: str, amount: float, currency: str | None = None) -> dict:
		currency = currency or self.currency
		capture_id = get_completed_capture_id(self.get_order(session_id))

		# ponytail: no PayPal-Request-Id here on purpose. A stable one would make PayPal replay the first
		# refund instead of taking a genuine second partial refund of the same amount; the caller's row
		# lock and refund ledger are what stop a double refund.
		refund = self.post(
			f"/v2/payments/captures/{capture_id}/refund",
			{
				"amount": {
					"currency_code": currency.upper(),
					"value": format_paypal_amount(amount, currency),
				}
			},
		)
		status = (refund.get("status") or "").strip().upper()
		if status not in PAYPAL_ACCEPTED_REFUND_STATUSES:
			frappe.throw(_("Refund failed with status: {0}").format(refund.get("status")))

		# The gateway's own echo is round-tripped back so a charge and its refund always agree to the last
		# minor unit, falling back to what we asked for when PayPal answers without an amount.
		echoed = (refund.get("amount") or {}).get("value")
		return {
			"refund_id": refund["id"],
			"status": status,
			"amount": flt(echoed) if echoed else flt(amount),
		}

	def post(self, endpoint: str, payload: dict, request_id: str | None = None) -> dict:
		url = f"{self.get_base_url()}{endpoint}"
		try:
			response = make_post_request(url, json=payload, headers=self.get_headers(request_id))
		except Exception as exception:
			self.throw_request_error(url, exception)
			# throw_request_error always raises today; the bare raise keeps `response` from ever being
			# read unbound if that stops being true.
			raise

		return self.read_response(url, response)

	def get_resource(self, endpoint: str) -> dict:
		url = f"{self.get_base_url()}{endpoint}"
		try:
			response = make_get_request(url, headers=self.get_headers())
		except Exception as exception:
			self.throw_request_error(url, exception)
			# throw_request_error always raises today; the bare raise keeps `response` from ever being
			# read unbound if that stops being true.
			raise

		return self.read_response(url, response)

	def throw_request_error(self, url: str, exception: Exception):
		"""PayPal explains a non-2xx in a JSON error body, so the status code alone never names it."""
		description = read_paypal_error(exception)
		self.log_request(url, error=description or exception)
		frappe.throw(_("PayPal rejected the request: {0}").format(description or _("unknown error")))

	def read_response(self, url: str, response) -> dict:
		if not isinstance(response, dict):
			self.log_request(url, error="PayPal returned a non-JSON body")
			frappe.throw(_("PayPal returned an unreadable response"))

		self.log_request(url, output=summarise_paypal_response(response))
		return response

	def log_request(self, url: str, output=None, error=None):
		# The headers carry the bearer token and the payload carries the shopper's name, email and
		# address, so only the endpoint and the outcome are logged.
		create_request_log(
			{"endpoint": url},
			service_name="PayPal",
			is_remote_request=True,
			reference_doctype=self.doctype,
			reference_docname=self.name,
			output=output,
			error=error,
			status="Failed" if error else "Completed",
		)


def validate_paypal_currency(currency: str):
	if (currency or "").strip().upper() not in PAYPAL_SUPPORTED_CURRENCIES:
		frappe.throw(
			_("PayPal cannot take payment in {0}. It supports {1}.").format(
				frappe.bold(currency), ", ".join(PAYPAL_SUPPORTED_CURRENCIES)
			)
		)


def get_paypal_exponent(currency: str) -> int:
	"""How many decimals PayPal will accept for this currency, which is not always ISO 4217's answer."""
	if (currency or "").strip().upper() in PAYPAL_WHOLE_AMOUNT_CURRENCIES:
		return 0
	return get_minor_unit_exponent(currency)


def format_paypal_amount(amount, currency: str) -> str:
	"""PayPal bills in major units as a decimal string, at the precision it accepts for that currency.

	A charge that cannot be expressed at that precision is refused rather than rounded: silently sending
	PayPal 1000 for a 1000.50 HUF order bills the shopper a different figure than the one they agreed to.
	"""
	exponent = get_paypal_exponent(currency)
	if flt(amount, get_minor_unit_exponent(currency)) != flt(amount, exponent):
		frappe.throw(
			_("PayPal does not accept decimals in {0}, so {1} cannot be charged.").format(
				frappe.bold(currency), amount
			)
		)
	return f"{flt(amount, exponent):.{exponent}f}"


def read_status(entity: dict) -> str:
	"""PayPal's status is uppercase, but normalising once keeps a stray " Completed " from slipping past."""
	return ((entity or {}).get("status") or "").strip().upper()


def get_redirect_url(order: dict) -> str:
	links = {link.get("rel"): link.get("href") for link in order.get("links") or []}
	for rel in PAYPAL_REDIRECT_LINK_RELS:
		if links.get(rel):
			return links[rel]

	# Without somewhere to send the shopper the order is unusable, and returning None would store a blank
	# `order_url` and strand them on a checkout button that does nothing.
	frappe.throw(_("PayPal created an order with no approval link"))


def get_completed_capture_id(order: dict) -> str:
	"""An order is not a charge, so a refund has to go against the capture that collected the money."""
	for purchase_unit in order.get("purchase_units") or []:
		for capture in ((purchase_unit.get("payments") or {}).get("captures")) or []:
			if read_status(capture) == PAYPAL_COMPLETED_STATUS:
				return capture["id"]

	frappe.throw(
		_(
			"PayPal has no completed capture for this order. An approved but uncaptured payment has to be"
			" captured or voided from the PayPal dashboard before it can be refunded."
		)
	)


def summarise_paypal_response(response: dict) -> dict:
	"""PayPal echoes the shopper's name and email address, so only the ids are logged."""
	return {"id": response.get("id"), "status": response.get("status")}


def read_paypal_error(exception: Exception) -> str | None:
	body = read_paypal_error_body(exception)
	if not body:
		return None
	details = body.get("details") or []
	description = (details[0] or {}).get("description") if details else None
	return description or body.get("message") or body.get("name")


def read_paypal_issue(exception: Exception) -> str | None:
	"""The machine-readable half of a PayPal error, which is what tells a race apart from a real failure."""
	details = (read_paypal_error_body(exception) or {}).get("details") or []
	if not details:
		return None
	return ((details[0] or {}).get("issue") or "").strip().upper() or None


def read_paypal_error_body(exception: Exception) -> dict | None:
	response = getattr(exception, "response", None)
	if response is None:
		return None
	try:
		body = response.json()
	except ValueError:
		return None
	return body if isinstance(body, dict) else None
