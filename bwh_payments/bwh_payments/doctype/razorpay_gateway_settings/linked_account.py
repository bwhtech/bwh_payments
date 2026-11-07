"""Razorpay Route linked accounts, onboarded through the v2 Accounts API."""

from typing import Literal

import frappe
from pydantic import BaseModel

from bwh_payments.base_class import LinkedAccountState
from bwh_payments.services.razorpay.base import BaseRazorpayClient

ACTIVATION_STATUSES = {
	"activated": "Active",
	"needs_clarification": "Needs Clarification",
	"suspended": "Suspended",
}
ROUTE_PRODUCT = {"product_name": "route", "tnc_accepted": True}


class RegisteredAddress(BaseModel):
	street1: str
	street2: str | None = None
	city: str
	state: str
	postal_code: str
	country: str = "IN"


class LinkedAccountDetails(BaseModel):
	"""What Razorpay needs to open and verify a Route linked account. Sent on, never stored."""

	email: str
	phone: str
	legal_business_name: str
	business_type: str
	contact_name: str
	category: str
	subcategory: str
	registered_address: RegisteredAddress
	business_pan: str | None = None
	gst: str | None = None
	stakeholder_name: str
	stakeholder_email: str
	stakeholder_pan: str | None = None
	bank_account_number: str
	ifsc_code: str
	beneficiary_name: str
	# The owner accepts Razorpay's terms; we only pass that on.
	terms_accepted: Literal[True]

	def account_payload(self) -> dict:
		"""What Razorpay lets us change after the account exists: not its email or business type."""
		legal_info = {"pan": self.business_pan, "gst": self.gst}
		return {
			"phone": self.phone,
			"legal_business_name": self.legal_business_name,
			"contact_name": self.contact_name,
			"profile": {
				"category": self.category,
				"subcategory": self.subcategory,
				"addresses": {"registered": self.registered_address.model_dump(exclude_none=True)},
			},
			"legal_info": {key: value for key, value in legal_info.items() if value},
		}

	def new_account_payload(self, reference_id: str) -> dict:
		return {
			**self.account_payload(),
			"email": self.email,
			"business_type": self.business_type,
			"type": "route",
			"reference_id": reference_id,
		}

	def stakeholder_payload(self) -> dict:
		payload = {"name": self.stakeholder_name, "email": self.stakeholder_email}
		if self.stakeholder_pan:
			payload["kyc"] = {"pan": self.stakeholder_pan}
		return payload

	def product_payload(self) -> dict:
		settlements = {
			"account_number": self.bank_account_number,
			"ifsc_code": self.ifsc_code,
			"beneficiary_name": self.beneficiary_name,
		}
		return {"settlements": settlements, "tnc_accepted": True}


class RazorpayLinkedAccount:
	"""Walks a Gateway Linked Account through Razorpay's onboarding, one resumable step at a time."""

	def __init__(self, client: BaseRazorpayClient, account):
		self.client = client
		self.account = account
		self.reference = frappe.parse_json(account.gateway_reference or "{}")

	def submit(self, details: dict):
		details = LinkedAccountDetails.model_validate(details)
		self.save_account(details)
		self.save_stakeholder(details)
		self.save_product(details)

	def save_account(self, details: LinkedAccountDetails):
		if self.account.account_id:
			self.client.update_account(self.account.account_id, details.account_payload())
			return
		created = self.client.create_account(details.new_account_payload(reference_id=self.account.name))
		self.store(account_id=created.id)

	def save_stakeholder(self, details: LinkedAccountDetails):
		if stakeholder_id := self.reference.get("stakeholder_id"):
			self.client.update_stakeholder(
				self.account.account_id, stakeholder_id, details.stakeholder_payload()
			)
			return
		created = self.client.create_stakeholder(self.account.account_id, details.stakeholder_payload())
		self.store_reference(stakeholder_id=created.id)

	def save_product(self, details: LinkedAccountDetails):
		if not (product_id := self.reference.get("product_id")):
			product_id = self.client.request_product(self.account.account_id, ROUTE_PRODUCT).id
			self.store_reference(product_id=product_id)
		self.client.update_product(self.account.account_id, product_id, details.product_payload())

	def get_state(self) -> LinkedAccountState:
		# An account added by hand from the dashboard has no product id to ask about.
		if not (product_id := self.reference.get("product_id")):
			return LinkedAccountState(status=self.account.status, gateway_status=self.account.gateway_status)

		product = self.client.get_product(self.account.account_id, product_id)
		return LinkedAccountState(
			status=ACTIVATION_STATUSES.get(product.activation_status, "Pending"),
			gateway_status=product.activation_status,
			requirements=product.requirements or [],
		)

	def store_reference(self, **ids):
		self.reference.update(ids)
		self.store(gateway_reference=frappe.as_json(self.reference))

	def store(self, **values):
		# Committed at once: Razorpay now holds this id, so a later step failing must not lose it.
		self.account.db_set(values)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
