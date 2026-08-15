# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils.data import flt

from bwh_payments.currency import get_minor_unit_exponent

REFUNDABLE_STATUSES = ("Paid", "Partially Refunded")
WEBHOOK_WRITABLE_STATUSES = ("Paid", "Not Paid", "Cancelled", "Expired")


class GatewayPaymentRequest(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		cancel_url: DF.Data | None
		company: DF.Link | None
		currency_code: DF.Link
		customer_address: DF.Link | None
		customer_email: DF.Data | None
		customer_forenames: DF.Data | None
		customer_phone: DF.Data | None
		customer_ref: DF.Link | None
		customer_surname: DF.Data | None
		erpnext_payment_request: DF.Link | None
		failure_url: DF.Data | None
		gateway: DF.Link
		gateway_transaction_ref: DF.Data | None
		last_webhook_event_id: DF.Data | None
		order_ref: DF.Data | None
		order_url: DF.SmallText | None
		ref_docname: DF.DynamicLink
		ref_doctype: DF.Link
		refund_amount: DF.Currency
		refund_id: DF.SmallText | None
		refund_payment_entries: DF.SmallText | None
		status: DF.Literal[
			"Pending", "Paid", "Not Paid", "Cancelled", "Expired", "Partially Refunded", "Refunded"
		]
		success_url: DF.Data | None
	# end: auto-generated types

	def validate(self):
		self.validate_amount_precision()

	def validate_amount_precision(self):
		"""Refuse a charge the document cannot store exactly, rather than quietly rounding it.

		Frappe derives a Currency field's precision from the site's default number format unless
		System Settings has `use_number_format_from_currency` on, so on a default site a 3-decimal
		currency such as KWD is rounded to 2 and the shopper is billed a different figure.
		"""
		exponent = get_minor_unit_exponent(self.currency_code)
		if flt(self.amount, self.precision("amount")) == flt(self.amount, exponent):
			return

		frappe.throw(
			_(
				"{0} amounts carry {1} decimals but this site stores currency to {2}. Enable"
				" <b>Use Number Format From Currency</b> in System Settings before taking {0} payments."
			).format(frappe.bold(self.currency_code), exponent, self.precision("amount")),
			title=_("Currency Precision Too Low"),
		)

	def before_save(self):
		if not self.order_ref:
			self.create_session()

	def get_gateway_settings(self):
		gateway_settings = frappe.get_cached_value(
			"Payment Gateway Profile", self.gateway, "gateway_settings"
		)
		return frappe.get_single(gateway_settings)

	def create_session(self):
		session = self.get_gateway_settings().create_session(
			flt(self.amount),
			self.currency_code,
			reference=self.name,
			customer=self.get_customer_details(),
		)
		self.order_ref = session.get("session_id")
		self.order_url = session.get("redirect_url")
		self.success_url = session.get("success_url")
		self.cancel_url = session.get("cancel_url")
		self.failure_url = session.get("failure_url")

	def get_customer_details(self) -> dict:
		address = None
		if self.customer_address:
			address_doc = frappe.get_cached_doc("Address", self.customer_address)
			address = {
				"line1": address_doc.address_line1,
				"city": address_doc.city,
				"country": address_doc.country,
			}
		return {
			"ref": self.customer_ref or "",
			"email": self.customer_email or "",
			"phone": self.customer_phone or "",
			"name": {
				"forenames": self.customer_forenames or "",
				"surname": self.customer_surname or "",
			},
			"address": address,
		}

	@frappe.whitelist()
	def sync_status(self):
		"""Re-read the authoritative status from the gateway. Never trust a browser redirect."""
		status = self.get_gateway_settings().get_payment_status(self.order_ref)

		# The gateway round-trip is slow, so the lock is taken after it and picks up whatever a webhook
		# committed meanwhile. Without it this save races that webhook into a TimestampMismatchError,
		# which the shopper sees as a 500 on the confirmation page.
		self.lock_refund_ledger()

		if flt(self.refund_amount) > 0:
			status = self.resolve_refund_status()
		elif self.status != "Pending" or status not in WEBHOOK_WRITABLE_STATUSES:
			return

		self.status = status
		self.save(ignore_permissions=True)

	def get_remaining_refundable_amount(self) -> float:
		# Refund arithmetic is pinned to the currency's own minor unit, not the field precision, so a
		# full refund always adds up to exactly what was charged.
		precision = get_minor_unit_exponent(self.currency_code)
		return flt(flt(self.amount, precision) - flt(self.refund_amount, precision), precision)

	def resolve_refund_status(self) -> str:
		# Anything at or above one minor unit is still real money the shopper is owed, and relabelling the
		# row Refunded would drop it out of REFUNDABLE_STATUSES and strand it forever.
		if self.get_remaining_refundable_amount() < 10 ** -get_minor_unit_exponent(self.currency_code):
			return "Refunded"
		return "Partially Refunded"

	def lock_refund_ledger(self):
		"""Take a row lock and refresh from it, so the over-refund guard cannot read a stale ledger.

		Without this two concurrent refunds both read refund_amount = 0, both pass the guard and both
		reach the gateway. The lock is held until the enclosing transaction commits.
		"""
		frappe.db.get_value(
			"Gateway Payment Request",
			self.name,
			["amount", "refund_amount", "status"],
			for_update=True,
		)
		self.reload()

	@frappe.whitelist()
	def refund(self, amount: float | None = None, payment_entry: str | None = None):
		self.lock_refund_ledger()

		if self.status not in REFUNDABLE_STATUSES:
			frappe.throw(
				_("Cannot refund a payment in status {0}").format(frappe.bold(_(self.status))),
				title=_("Refund Not Allowed"),
			)

		if payment_entry:
			payment_entry = get_original_payment_entry(payment_entry)
		if payment_entry and payment_entry in self.get_refunded_payment_entries():
			return self.get_refund_ledger()

		precision = get_minor_unit_exponent(self.currency_code)
		remaining = self.get_remaining_refundable_amount()
		# `flt(amount, precision) or remaining` refunded the whole balance for any sub-minor request,
		# because flt(0.004, 2) is falsy. Only an omitted amount may mean "everything left".
		amount = remaining if amount is None else flt(amount, precision)

		if amount <= 0:
			frappe.throw(_("Refund amount must be greater than zero"))
		if amount > remaining:
			frappe.throw(
				_("Refund amount ({0}) exceeds the remaining refundable amount ({1})").format(
					amount, remaining
				)
			)

		# ponytail: the intent log shares this transaction, so a crash between the gateway call and the
		# commit still loses the record; reconcile from the storefront Orphaned Payments report. Committing
		# it first would release the row lock above and re-open the double-refund window.
		request_log = create_request_log(
			{
				"gateway_payment_request": self.name,
				"order_ref": self.order_ref,
				"amount": amount,
				"currency": self.currency_code,
			},
			service_name=f"{self.gateway} Refund",
			reference_doctype=self.doctype,
			reference_docname=self.name,
		)

		try:
			result = self.get_gateway_settings().refund_payment(self.order_ref, amount, self.currency_code)
		except Exception:
			request_log.db_set("status", "Failed", update_modified=False)
			raise

		self.append_refund_id((result or {}).get("refund_id"))
		self.refund_amount = flt(flt(self.refund_amount, precision) + amount, precision)
		self.status = self.resolve_refund_status()
		if payment_entry:
			self.append_refunded_payment_entry(payment_entry)
		self.save(ignore_permissions=True)

		request_log.db_set("status", "Completed", update_modified=False)
		return self.get_refund_ledger()

	def get_refund_ledger(self) -> dict:
		return {"refund_amount": self.refund_amount, "status": self.status, "refund_id": self.refund_id}

	def append_refund_id(self, refund_id: str | None):
		# Repeated partial refunds each return a distinct id, so append rather than overwrite — the ids are
		# the only way to reconcile against a gateway statement.
		if not refund_id:
			return
		existing_ids = [entry for entry in (self.refund_id or "").split(",") if entry]
		existing_ids.append(refund_id)
		self.refund_id = ",".join(existing_ids)

	def get_refunded_payment_entries(self) -> list[str]:
		return [entry for entry in (self.refund_payment_entries or "").split(",") if entry]

	def append_refunded_payment_entry(self, payment_entry: str):
		entries = self.get_refunded_payment_entries()
		entries.append(payment_entry)
		self.refund_payment_entries = ",".join(entries)

	def apply_webhook_status(self, status: str, event_id: str | None = None) -> bool:
		"""Apply a verified gateway status. Return False when the event is a replay or not applicable."""
		self.lock_refund_ledger()

		if event_id and self.last_webhook_event_id == event_id:
			return False
		if self.status != "Pending":
			return False
		if status not in WEBHOOK_WRITABLE_STATUSES:
			return False

		self.status = status
		self.last_webhook_event_id = event_id
		self.save(ignore_permissions=True)
		return True


def get_original_payment_entry(payment_entry: str | int) -> str:
	"""Walk a Payment Entry back to the one it was first amended from.

	Cancelling and amending re-issues the entry under a new name, so keying the refund ledger on the
	current name lets the same refund reach the gateway again for every amendment.
	"""
	name = payment_entry
	while amended_from := frappe.db.get_value("Payment Entry", name, "amended_from"):
		name = amended_from
	return name


def refund_on_payment_entry(doc, method=None):
	"""Mirror an outgoing refund Payment Entry back to the gateway that took the money."""
	if doc.payment_type != "Pay" or not doc.reference_no:
		return

	request_name = frappe.db.get_value(
		"Gateway Payment Request",
		{"order_ref": doc.reference_no, "status": ["in", REFUNDABLE_STATUSES]},
		"name",
	)
	if not request_name:
		return

	payment_request = frappe.get_doc("Gateway Payment Request", request_name)
	# A supplier payment can carry any reference string; only refund when the Payment Entry was actually
	# settled through this gateway's Mode of Payment. A blank mode is not a match — treating it as one
	# refunds real money off nothing more than a coincidental reference_no.
	if doc.mode_of_payment != payment_request.gateway:
		return

	payment_request.refund(flt(doc.paid_amount), payment_entry=doc.name)
