"""A stand-in for PayPal's HTTP transport.

PayPal is driven over raw HTTP here, not an SDK, so the seam is the two request helpers the controller
imports at module scope. Patch these over `make_post_request` / `make_get_request` *inside*
`paypal_gateway_settings` and the controller runs its real code — amount formatting, URL building, status
mapping, capture-race handling and webhook verification — against a recording fake instead of the network.

PayPal verifies a webhook by calling itself back, so unlike the Stripe and Razorpay fakes this one also
stands in for the verifier. `next_verification_status` is what a test turns to make a delivery fail
verification the way a forged one would.
"""

import json
from typing import ClassVar

import frappe

ORDERS_ENDPOINT = "/v2/checkout/orders"
TOKEN_ENDPOINT = "/v1/oauth2/token"
VERIFY_ENDPOINT = "/v1/notifications/verify-webhook-signature"

# The five headers PayPal signs a delivery with, in the canonical title-case werkzeug hands over.
SIGNATURE_HEADERS = {
	"Paypal-Auth-Algo": "SHA256withRSA",
	"Paypal-Cert-Url": "https://api.sandbox.paypal.com/cert.pem",
	"Paypal-Transmission-Id": "transmission-test-0001",
	"Paypal-Transmission-Sig": "signature-test-0001",
	"Paypal-Transmission-Time": "2026-09-03T00:00:00Z",
}


class FakePayPalError(Exception):
	"""Carries a `.response` the way requests' HTTPError does, so `read_paypal_issue` can read it."""

	def __init__(self, body: dict, status_code: int = 422):
		super().__init__(body.get("message") or body.get("name") or "PayPal error")
		self.response = FakePayPalErrorResponse(body, status_code)


class FakePayPalErrorResponse:
	def __init__(self, body: dict, status_code: int):
		self._body = body
		self.status_code = status_code

	def json(self) -> dict:
		return self._body


class FakePayPal:
	"""Records what the controller sent so a test can assert on the exact amount string."""

	created_orders: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	verified_events: ClassVar[list[dict]] = []
	captured_orders: ClassVar[list[str]] = []
	token_requests: ClassVar[list[str]] = []
	orders: ClassVar[dict[str, dict]] = {}
	next_verification_status = "SUCCESS"
	next_refund_status = "COMPLETED"
	# Set to make the next capture answer the way PayPal answers the loser of a capture race.
	next_capture_already_captured = False

	@classmethod
	def reset(cls):
		cls.created_orders = []
		cls.created_refunds = []
		cls.verified_events = []
		cls.captured_orders = []
		cls.token_requests = []
		cls.orders = {}
		cls.next_verification_status = "SUCCESS"
		cls.next_refund_status = "COMPLETED"
		cls.next_capture_already_captured = False
		# The token is cached across calls, so a suite that does not clear it never exercises the fetch.
		frappe.cache.delete_value("paypal_access_token::Sandbox")
		frappe.cache.delete_value("paypal_access_token::Live")

	@classmethod
	def register_order(cls, status: str = "CREATED", order_id: str | None = None, **fields) -> str:
		"""Put an order on the fake in a given status and return its id."""
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		order_id = order_id or f"PAYPAL{frappe.generate_hash(length=12).upper()}"
		cls.orders[order_id] = {
			"id": order_id,
			"status": status,
			"links": [
				{"rel": "self", "href": f"https://api.paypal.test/v2/checkout/orders/{order_id}"},
				{"rel": "payer-action", "href": f"https://paypal.test/checkoutnow?token={order_id}"},
			],
			"purchase_units": [{"payments": {"captures": []}}],
			**fields,
		}
		return order_id

	@classmethod
	def add_capture(cls, order_id: str, status: str = "COMPLETED", capture_id: str | None = None) -> str:
		capture_id = capture_id or f"CAPTURE{frappe.generate_hash(length=12).upper()}"
		purchase_units = cls.orders[order_id].setdefault("purchase_units", [{}])
		payments = purchase_units[0].setdefault("payments", {})
		payments.setdefault("captures", []).append({"id": capture_id, "status": status})
		return capture_id

	# --- transport --------------------------------------------------------

	@classmethod
	def post(cls, url, auth=None, json=None, data=None, headers=None, **kwargs):
		payload = json or {}
		endpoint = url.split(".com", 1)[-1]

		if endpoint == TOKEN_ENDPOINT:
			cls.token_requests.append(data)
			return {"access_token": f"A21AA{frappe.generate_hash(length=12)}", "expires_in": 32400}

		if endpoint == VERIFY_ENDPOINT:
			cls.verified_events.append(payload)
			return {"verification_status": cls.next_verification_status}

		if endpoint == ORDERS_ENDPOINT:
			cls.created_orders.append(payload)
			amount = (payload["purchase_units"][0] or {}).get("amount") or {}
			order_id = cls.register_order(status="CREATED", amount=amount)
			return dict(cls.orders[order_id])

		if endpoint.startswith(f"{ORDERS_ENDPOINT}/") and endpoint.endswith("/capture"):
			return cls.capture(endpoint.split("/")[4])

		if endpoint.startswith("/v2/payments/captures/") and endpoint.endswith("/refund"):
			cls.created_refunds.append({"capture_id": endpoint.split("/")[4], **payload})
			return {
				"id": f"REFUND{frappe.generate_hash(length=12).upper()}",
				"status": cls.next_refund_status,
				# PayPal echoes the amount back as the same major-unit decimal string it was sent.
				"amount": payload.get("amount"),
			}

		raise AssertionError(f"unexpected PayPal POST to {url}")

	@classmethod
	def get(cls, url, auth=None, headers=None, **kwargs):
		endpoint = url.split(".com", 1)[-1]

		if endpoint.startswith(f"{ORDERS_ENDPOINT}/"):
			order = cls.orders.get(endpoint.rsplit("/", 1)[-1])
			if not order:
				raise FakePayPalError(
					{
						"name": "RESOURCE_NOT_FOUND",
						"message": "The specified resource does not exist.",
						"details": [{"issue": "INVALID_RESOURCE_ID"}],
					},
					status_code=404,
				)
			return dict(order)

		raise AssertionError(f"unexpected PayPal GET to {url}")

	@classmethod
	def capture(cls, order_id: str) -> dict:
		if cls.next_capture_already_captured:
			raise FakePayPalError(
				{
					"name": "UNPROCESSABLE_ENTITY",
					"message": "The requested action could not be performed.",
					"details": [
						{"issue": "ORDER_ALREADY_CAPTURED", "description": "Order already captured."}
					],
				}
			)

		order = cls.orders.get(order_id)
		if not order:
			raise AssertionError(f"capture of unknown PayPal order {order_id}")

		cls.captured_orders.append(order_id)
		order["status"] = "COMPLETED"
		cls.add_capture(order_id)
		return dict(order)


def build_order_approved_event(order_id: str, event_id: str = "WH-TEST-APPROVED-0001") -> bytes:
	return json.dumps(
		{
			"id": event_id,
			"event_type": "CHECKOUT.ORDER.APPROVED",
			"resource": {"id": order_id, "status": "APPROVED"},
		}
	).encode()


def build_capture_completed_event(order_id: str, event_id: str = "WH-TEST-CAPTURE-0001") -> bytes:
	"""`resource.id` is the capture; the order id only ever appears in supplementary data."""
	return json.dumps(
		{
			"id": event_id,
			"event_type": "PAYMENT.CAPTURE.COMPLETED",
			"resource": {
				"id": f"CAPTURE{frappe.generate_hash(length=8).upper()}",
				"status": "COMPLETED",
				"supplementary_data": {"related_ids": {"order_id": order_id}},
			},
		}
	).encode()
