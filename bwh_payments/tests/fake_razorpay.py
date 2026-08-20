"""A stand-in for Razorpay's HTTP transport.

Razorpay is driven over raw HTTP here, not an SDK, so the seam is the two request helpers the controller
imports at module scope. Patch these over `make_post_request` / `make_get_request` *inside*
`razorpay_gateway_settings` and the controller runs its real code — minor-unit conversion, URL building,
status mapping, signature verification — against a recording fake instead of the network.
"""

import hashlib
import hmac
import json
from typing import ClassVar

import frappe

PAYMENT_LINKS_ENDPOINT = "/payment_links"


class FakeRazorpay:
	"""Records what the controller sent so a test can assert on the exact minor-unit amount."""

	created_links: ClassVar[list[dict]] = []
	created_refunds: ClassVar[list[dict]] = []
	links: ClassVar[dict[str, dict]] = {}
	next_refund_status = "processed"

	@classmethod
	def reset(cls):
		cls.created_links = []
		cls.created_refunds = []
		cls.links = {}
		cls.next_refund_status = "processed"

	@classmethod
	def register_link(cls, status: str = "created", link_id: str | None = None, **fields) -> str:
		"""Put a payment link on the fake in a given status and return its id."""
		# Unique per call: the doctype enforces a unique order_ref and rows outlive a single test.
		link_id = link_id or f"plink_{frappe.generate_hash(length=12)}"
		cls.links[link_id] = {
			"id": link_id,
			"status": status,
			"short_url": f"https://rzp.io/i/{link_id}",
			"payments": [],
			**fields,
		}
		return link_id

	@classmethod
	def add_payment(cls, link_id: str, status: str, payment_id: str | None = None) -> str:
		payment_id = payment_id or f"pay_{frappe.generate_hash(length=12)}"
		cls.links[link_id].setdefault("payments", []).append({"payment_id": payment_id, "status": status})
		return payment_id

	# --- transport --------------------------------------------------------

	@classmethod
	def post(cls, url, auth=None, json=None, headers=None, **kwargs):
		payload = json or {}
		endpoint = url.split("/v1", 1)[-1]

		if endpoint == PAYMENT_LINKS_ENDPOINT:
			cls.created_links.append(payload)
			link_id = cls.register_link(
				status="created", amount=payload.get("amount"), currency=payload.get("currency")
			)
			return dict(cls.links[link_id])

		if endpoint.startswith("/payments/") and endpoint.endswith("/refund"):
			cls.created_refunds.append({"payment_id": endpoint.split("/")[2], **payload})
			return {
				"id": f"rfnd_{frappe.generate_hash(length=12)}",
				"status": cls.next_refund_status,
				# Razorpay echoes the amount back in minor units.
				"amount": payload.get("amount"),
			}

		raise AssertionError(f"unexpected Razorpay POST to {url}")

	@classmethod
	def get(cls, url, auth=None, headers=None, **kwargs):
		endpoint = url.split("/v1", 1)[-1]

		if endpoint.startswith(f"{PAYMENT_LINKS_ENDPOINT}/"):
			link_id = endpoint.rsplit("/", 1)[-1]
			link = cls.links.get(link_id)
			if not link:
				return {"error": {"code": "BAD_REQUEST_ERROR", "description": "payment link not found"}}
			return dict(link)

		raise AssertionError(f"unexpected Razorpay GET to {url}")


def sign_razorpay_payload(payload: bytes, secret: str) -> str:
	"""Build a genuine X-Razorpay-Signature so signature verification is exercised, not bypassed."""
	return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def build_payment_link_paid_event(
	link_id: str, status: str = "paid", event: str = "payment_link.paid"
) -> bytes:
	return json.dumps(
		{
			"entity": "event",
			"event": event,
			"payload": {"payment_link": {"entity": {"id": link_id, "status": status}}},
		}
	).encode()
