# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.data import cstr, flt

from bwh_payments.base_class import CheckoutSession, RefundResult
from bwh_payments.bwh_payments.checkout import get_return_url
from bwh_payments.bwh_payments.utils import is_local_path
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
		self.validate_new_checkout_is_pending()
		self.validate_redirect_to()
		self.validate_amount_precision()

	def validate_redirect_to(self):
		if self.redirect_to and not is_local_path(self.redirect_to):
			frappe.throw(_("Redirect To must be a path on this site, such as /orders/1"))

	def validate_new_checkout_is_pending(self):
		# A request without order_ref opens a real checkout, so it cannot already be settled.
		if self.is_new() and not self.order_ref and self.status != "Pending":
			frappe.throw(_("A new checkout must start as Pending"))

	def validate_amount_precision(self):
		"""Without `use_number_format_from_currency`, a default site rounds KWD to 2 decimals."""
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
		elif not self.is_new() and self.has_value_changed("status") and self.status == "Paid":
			self.gateway_transaction_ref = self.gateway_transaction_ref or self.fetch_transaction_ref()

	def on_update(self):
		# frappe/payments convention, so apps written for it settle orders unchanged.
		# An insert that is already Paid is an import of a payment settled elsewhere.
		if not self.flags.in_insert and self.has_value_changed("status") and self.status == "Paid":
			frappe.get_doc(self.ref_doctype, self.ref_docname).run_method(
				"on_payment_authorized", "Completed"
			)

	def get_gateway_settings(self):
		gateway_settings = frappe.get_cached_value(
			"Payment Gateway Profile", self.gateway, "gateway_settings"
		)
		if not gateway_settings:
			frappe.throw(
				_("{0} is kept for history only and cannot be used").format(frappe.bold(self.gateway))
			)
		return frappe.get_single(gateway_settings)

	def create_session(self):
		# Passed only when set, so gateways written before return_url keep working.
		return_url = {"return_url": get_return_url(self.name)} if self.redirect_to else {}
		session = CheckoutSession.model_validate(
			self.get_gateway_settings().create_session(
				flt(self.amount),
				self.currency_code,
				reference=self.name,
				customer=self.get_customer_details(),
				**return_url,
			)
		)
		self.order_ref = session.session_id
		self.order_url = session.redirect_url
		self.success_url = session.success_url
		self.cancel_url = session.cancel_url
		self.failure_url = session.failure_url

	def fetch_transaction_ref(self) -> str | None:
		try:
			return self.get_gateway_settings().get_transaction_ref(self.order_ref)
		except Exception:
			# Only refunds raised outside this app need it, so a failed lookup must not block the payment.
			frappe.clear_last_message()
			frappe.log_error(title=_("Could not read the settled transaction for {0}").format(self.name))
			return None

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
		# Asking the gateway may capture, so a request that is no longer Pending never asks.
		status = None
		if self.status == "Pending":
			status = self.get_gateway_settings().get_payment_status(self.order_ref)

		# Locked after the slow gateway call, so a webhook committed meanwhile cannot cause a
		# TimestampMismatchError.
		self.lock_row()

		if flt(self.refund_amount) > 0:
			status = self.resolve_refund_status()
		elif self.status != "Pending" or status not in WEBHOOK_WRITABLE_STATUSES:
			return

		self.status = status
		self.save(ignore_permissions=True)

	def release_if_unpaid(self) -> bool:
		self.sync_status()
		if self.status != "Pending":
			return self.status in ("Cancelled", "Expired", "Not Paid")

		if not self.get_gateway_settings().cancel_session(self.order_ref):
			self.sync_status()
			return False

		self.lock_row()
		# A recorded transaction means the shopper was charged, even if the request is not Paid yet.
		if self.status != "Pending" or self.gateway_transaction_ref:
			return False

		self.status = "Cancelled"
		self.save(ignore_permissions=True)
		return True

	def get_unrefunded_amount(self) -> float:
		precision = get_minor_unit_exponent(self.currency_code)
		return flt(flt(self.amount, precision) - flt(self.refund_amount, precision), precision)

	def get_pending_refund_amount(self) -> float:
		pending = frappe.get_all(
			"Gateway Refund", filters={"payment_request": self.name, "status": "Pending"}, pluck="amount"
		)
		return sum(flt(amount) for amount in pending)

	def get_remaining_refundable_amount(self) -> float:
		precision = get_minor_unit_exponent(self.currency_code)
		return flt(self.get_unrefunded_amount() - self.get_pending_refund_amount(), precision)

	def resolve_refund_status(self) -> str:
		if self.get_unrefunded_amount() < 10 ** -get_minor_unit_exponent(self.currency_code):
			return "Refunded"
		return "Partially Refunded"

	def lock_row(self):
		"""Row lock plus reload, so concurrent refunds cannot both pass the over-refund guard."""
		frappe.db.get_value(
			"Gateway Payment Request",
			self.name,
			["amount", "refund_amount", "status"],
			for_update=True,
		)
		self.reload()

	def refund(self, amount: float | None = None, payment_entry: str | None = None) -> dict:
		self.create_refund(amount, payment_entry=payment_entry)
		self.reload()
		return self.get_refund_summary()

	def create_refund(
		self, amount: float | None = None, reference: str | None = None, payment_entry: str | None = None
	):
		"""Refund `amount` (the whole remaining balance when None) and return the Gateway Refund."""
		self.lock_row()

		if self.status not in REFUNDABLE_STATUSES:
			frappe.throw(
				_("Cannot refund a payment in status {0}").format(frappe.bold(_(self.status))),
				title=_("Refund Not Allowed"),
			)

		if payment_entry:
			payment_entry = get_original_payment_entry(payment_entry)
			if payment_entry in self.get_refunded_payment_entries():
				return None
			if existing := self.get_refund_for_payment_entry(payment_entry):
				return existing

		precision = get_minor_unit_exponent(self.currency_code)
		remaining = self.get_remaining_refundable_amount()
		# `is None`, not falsy: flt(0.004, 2) is 0 and must not mean "refund everything".
		amount = remaining if amount is None else flt(amount, precision)

		if amount <= 0:
			frappe.throw(_("Refund amount must be greater than zero"))
		if amount > remaining:
			frappe.throw(
				_("Refund amount ({0}) exceeds the remaining refundable amount ({1})").format(
					amount, remaining
				)
			)

		refund = frappe.get_doc(
			{
				"doctype": "Gateway Refund",
				"payment_request": self.name,
				"amount": amount,
				"reference": reference,
				"payment_entry": payment_entry,
			}
		).insert(ignore_permissions=True)
		# Committed before the slow gateway call, which releases the row lock: from here the Pending
		# refund holds its amount against any concurrent refund.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

		try:
			result = self.get_gateway_settings().refund_payment(self.order_ref, amount, self.currency_code)
		except Exception as error:
			refund.mark_failed(str(error))
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
			raise

		refund.apply_gateway_result(RefundResult.model_validate(result or {}))
		return refund

	@frappe.whitelist()
	def sync_refunds(self):
		# Not check_permission: it is skipped when an earlier save set flags.ignore_permissions.
		frappe.has_permission(self.doctype, "write", self, throw=True)
		self.update_pending_refunds()

	def update_pending_refunds(self):
		"""Ask the gateway about every Pending refund, for when a refund webhook never arrived."""
		settings = self.get_gateway_settings()
		for name in frappe.get_all(
			"Gateway Refund",
			filters={"payment_request": self.name, "status": "Pending", "refund_id": ["is", "set"]},
			pluck="name",
		):
			refund = frappe.get_doc("Gateway Refund", name)
			result = settings.get_refund_status(self.order_ref, refund.refund_id)
			if result:
				refund.apply_gateway_status(RefundResult.model_validate(result).status)

		self.record_unknown_refunds(settings)

	def record_unknown_refunds(self, settings):
		"""Record refunds raised outside this app whose webhook never arrived."""
		# One of our refunds still waiting for its id may be among them; recording it now would count it twice.
		if frappe.db.exists(
			"Gateway Refund",
			{"payment_request": self.name, "refund_id": ["is", "not set"], "status": "Pending"},
		):
			return

		known = set(frappe.get_all("Gateway Refund", {"payment_request": self.name}, pluck="refund_id"))
		for result in settings.list_refunds(self.order_ref) or []:
			result = RefundResult.model_validate(result)
			if result.refund_id and result.refund_id not in known:
				self.record_external_refund(result.refund_id, result.amount).apply_gateway_status(
					result.status
				)

	def record_external_refund(self, refund_id: str, amount: float):
		"""Record a refund raised outside this app, e.g. from the gateway's dashboard."""
		return frappe.get_doc(
			{
				"doctype": "Gateway Refund",
				"payment_request": self.name,
				"refund_id": refund_id,
				"amount": amount,
			}
		).insert(ignore_permissions=True)

	def get_refund_for_payment_entry(self, payment_entry: str):
		name = frappe.db.get_value(
			"Gateway Refund", {"payment_entry": payment_entry, "status": ["!=", "Failed"]}, "name"
		)
		return frappe.get_doc("Gateway Refund", name) if name else None

	def add_processed_refund(self, refund):
		self.lock_row()
		precision = get_minor_unit_exponent(self.currency_code)
		self.refund_amount = flt(flt(self.refund_amount, precision) + flt(refund.amount), precision)
		self.append_refund_id(refund.refund_id)
		self.status = self.resolve_refund_status()
		self.save(ignore_permissions=True)

	def get_refund_summary(self) -> dict:
		return {"refund_amount": self.refund_amount, "status": self.status, "refund_id": self.refund_id}

	def append_refund_id(self, refund_id: str | None):
		if not refund_id:
			return
		existing_ids = [entry for entry in (self.refund_id or "").split(",") if entry]
		existing_ids.append(refund_id)
		self.refund_id = ",".join(existing_ids)

	def get_refunded_payment_entries(self) -> list[str]:
		"""Payment Entries refunded before Gateway Refund kept the ledger."""
		return [entry for entry in (self.refund_payment_entries or "").split(",") if entry]

	def apply_webhook_status(
		self, status: str, event_id: str | None = None, transaction_ref: str | None = None
	) -> bool:
		self.lock_row()

		if event_id and self.last_webhook_event_id == event_id:
			return False
		# Kept even when a status sync settled the request first: some gateways (M-Pesa) name the
		# settled payment only in their webhook, and refunds need it.
		if status == "Paid" and transaction_ref and not self.gateway_transaction_ref:
			self.db_set("gateway_transaction_ref", transaction_ref)
		if self.status != "Pending":
			return False
		if status not in WEBHOOK_WRITABLE_STATUSES:
			return False

		self.status = status
		self.last_webhook_event_id = event_id
		self.save(ignore_permissions=True)
		return True


def get_original_payment_entry(payment_entry: str | int) -> str:
	"""Key the refund ledger on the first entry, so an amendment cannot refund twice.

	Returns `str`: an autoincrement `int` name would never match the comma-joined ledger.
	"""
	name = payment_entry
	while amended_from := frappe.db.get_value("Payment Entry", name, "amended_from"):
		name = amended_from
	return cstr(name)


def refund_on_payment_entry(doc, method=None):
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
	# A supplier payment can share the reference_no by chance; the Mode of Payment must match too.
	if doc.mode_of_payment != payment_request.gateway:
		return

	payment_request.refund(flt(doc.paid_amount), payment_entry=doc.name)
